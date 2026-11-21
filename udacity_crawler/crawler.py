"""抓取主流程：enrollment → course → lesson → chapter，逐层落盘并按标题跳过。"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PWTimeout

from . import catalog
from .browser import BrowserSession, dismiss_modals, safe_click
from .config import (
    CONTENT_TIMEOUT,
    NAV_TIMEOUT,
    RENDER_TIMEOUT,
    SETTLE_MS,
    Settings,
    normalize_title,
    sanitize,
)
from .content import extract_content, localize_images, prepare_page, render_chapter
from .logs import log
from .state import LocalIndex, StateStore
from .ui import format_duration
from .workers import ResourceWorker, WorkspaceWorker

_CHAPTER_ITEMS = (
    '#lesson-panel .chakra-accordion__item:has(> button[aria-expanded="true"]) '
    '.chakra-accordion__panel ul[aria-label^="Concepts"] > li'
)

_READ_CHAPTERS_JS = """(selector) => [...document.querySelectorAll(selector)].map(li => ({
    title: ((li.querySelector('p') || li).innerText || '').trim().split(String.fromCharCode(10))[0],
    active: li.getAttribute('data-active') === 'true',
}))"""

_ACTIVE_INDEX_JS = """(selector) => [...document.querySelectorAll(selector)]
    .findIndex(li => li.getAttribute('data-active') === 'true')"""

_CONTENT_READY_JS = """() => {
    const scroll = document.querySelector('.lesson-content-scroll-container');
    if (!scroll) return false;
    if (scroll.querySelector('[data-woolf-resource]')) return true;
    return (scroll.innerText || '').trim().length > 0;
}"""

_CONCEPT_KEY_JS = "() => new URLSearchParams(location.search).get('conceptKey') || ''"

_EXERCISE_RE = re.compile(r"exercis", re.IGNORECASE)


@dataclass
class RunStats:
    lessons: int = 0
    chapters_new: int = 0
    chapters_skipped: int = 0
    chapters_failed: int = 0
    images: int = 0
    started: float = 0.0

    def summary(self, resources_queued: int, notebooks_queued: int, failures: int) -> str:
        lines = [
            "",
            "=" * 56,
            f" 完成 · 用时 {format_duration(time.monotonic() - self.started)}",
            f" lesson {self.lessons} 个 · chapter 新增 {self.chapters_new} / "
            f"已存在 {self.chapters_skipped} / 失败 {self.chapters_failed}",
            f" 图片 {self.images} 张 · 资源任务 {resources_queued} 个 · notebook 任务 {notebooks_queued} 个",
        ]
        if failures:
            lines.append(f" 后台任务失败 {failures} 个（详见上方日志）")
        lines.append("=" * 56)
        return "\n".join(lines)


class Crawler:
    def __init__(self, session: BrowserSession, settings: Settings,
                 state: StateStore, index: LocalIndex) -> None:
        self.session = session
        self.settings = settings
        self.state = state
        self.index = index
        self.page: Page | None = None
        self.resources: ResourceWorker | None = None
        self.notebooks: WorkspaceWorker | None = None
        self.stats = RunStats(started=time.monotonic())
        self.resources_queued = 0
        self.notebooks_queued = 0
        self._lesson_title = ""

    # ---------------------------------------------------------------- 对外入口
    async def run(self, enrollments: list[catalog.Enrollment], page: Page | None = None) -> None:
        # 复用已有 page：持久化 context 里关掉最后一个标签页会直接退出浏览器
        self.page = page if page and not page.is_closed() else await self.session.new_page()
        self.resources = ResourceWorker(self.session, self.settings, self.index, self.state)
        self.notebooks = WorkspaceWorker(self.session, self.settings, self.index, self.state)
        if self.settings.resources:
            await self.resources.start()
        if self.settings.notebooks:
            await self.notebooks.start()

        counter = 0
        try:
            for enrollment in enrollments:
                counter = await self._crawl_enrollment(enrollment, counter)
                if self.settings.max_lessons and counter >= self.settings.max_lessons:
                    break
        finally:
            await self.resources.close()
            await self.notebooks.close()
        failures = (self.resources.failures if self.resources else 0) + \
                   (self.notebooks.failures if self.notebooks else 0)
        log.info("%s", self.stats.summary(self.resources_queued, self.notebooks_queued, failures))

    # -------------------------------------------------------------- 层级遍历
    async def _crawl_enrollment(self, enrollment: catalog.Enrollment, counter: int) -> int:
        enrollment_dir = self.index.find_dir(self.settings.out, enrollment.title) \
            or self.settings.out / sanitize(enrollment.title, enrollment.key)
        enrollment_dir.mkdir(parents=True, exist_ok=True)
        log.info("=== %s (%s) ===", enrollment.title, enrollment.key)
        log.info("目录：%s", enrollment_dir)

        try:
            courses = await catalog.fetch_curriculum(self.page, enrollment)
        except Exception as exc:
            log.error("读取目录失败：%s", exc)
            return counter
        if not courses:
            log.error("没有解析到任何 course，页面结构可能已变化：%s", enrollment.title)
            return counter
        self.state.record_enrollment(enrollment.key, enrollment.title,
                                     sum(len(c.lessons) for c in courses))

        for course in courses:
            course_dir = self.index.course_dir(enrollment_dir, course.title, create=False)
            for lesson in course.lessons:
                if self.settings.only_lesson and lesson.key != self.settings.only_lesson:
                    continue
                if self.settings.max_lessons and counter >= self.settings.max_lessons:
                    return counter
                counter += 1
                self.stats.lessons += 1
                try:
                    await self._crawl_lesson(enrollment, course, lesson, course_dir)
                except Exception as exc:  # 单个 lesson 出错不影响后续
                    log.error("lesson 失败（继续）：%s / %s：%s", course.title, lesson.title, exc)
                if not self.settings.parallel:
                    await self.resources.drain()
                    await self.notebooks.drain()
        return counter

    async def _crawl_lesson(self, enrollment: catalog.Enrollment, course: catalog.Course,
                            lesson: catalog.Lesson, course_dir: Path) -> None:
        lesson_dir = self.index.lesson_dir(course_dir, lesson.title)
        state_key = f"{enrollment.key}:{lesson.key}"
        log.info("— %s / %s", course.title, lesson.title)

        if self.settings.resources:
            if await self.resources.submit_lesson(lesson.url, lesson_dir, state_key):
                self.resources_queued += 1

        chapters = await self._open_lesson(lesson)
        if not chapters:
            log.warning("  lesson 面板未读到 chapter，跳过：%s", lesson.title)
            return

        existing = self.index.chapters(lesson_dir)
        pending: list[tuple[int, bool]] = []
        for i, item in enumerate(chapters):
            title = item["title"]
            has_html = normalize_title(title) in existing
            needs_notebook = (self.settings.notebooks and _EXERCISE_RE.search(title)
                              and not self.index.notebook_ready(lesson_dir, title))
            if self.settings.overwrite:
                pending.append((i, True))
            elif not has_html or needs_notebook:
                pending.append((i, not has_html))
        skipped = len(chapters) - len(pending)
        self.stats.chapters_skipped += skipped
        if self.settings.max_chapters:
            pending = pending[: self.settings.max_chapters]
        if not pending:
            log.info("  %d 个 chapter 均已存在，跳过正文抓取", len(chapters))
            return
        log.info("  chapter %d 个：已存在 %d，待处理 %d", len(chapters), skipped, len(pending))

        breadcrumb = [enrollment.title, course.title, lesson.title]
        for position, needs_html in pending:
            await self._crawl_chapter(lesson, lesson_dir, state_key, breadcrumb,
                                      position, chapters[position], len(chapters), needs_html)

    async def _crawl_chapter(self, lesson: catalog.Lesson, lesson_dir: Path, state_key: str,
                             breadcrumb: list[str], position: int, chapter: dict,
                             total: int, needs_html: bool) -> None:
        title = chapter["title"] or f"Chapter {position + 1}"
        label = f"[{position + 1}/{total}] {title}"
        attempts = max(1, self.settings.retries)
        for attempt in range(1, attempts + 1):
            try:
                await self._activate_chapter(position)
                chapter_url = self.page.url
                if needs_html:
                    await prepare_page(self.page)
                    content = await extract_content(self.page)
                    path, images = await self._write_chapter(
                        lesson_dir, position + 1, title, content, chapter_url, breadcrumb)
                    self.stats.chapters_new += 1
                    suffix = f" ({images} 图)" if images else ""
                    log.info("  %s -> %s%s", label, path.name, suffix)
                else:
                    log.info("  %s 正文已存在，只补 notebook", label)

                if self.settings.notebooks and _EXERCISE_RE.search(title):
                    if await self.notebooks.submit_chapter(chapter_url, lesson_dir, title, state_key):
                        self.notebooks_queued += 1
                return
            except Exception as exc:
                if attempt >= attempts:
                    self.stats.chapters_failed += 1
                    log.warning("  %s 处理失败（已重试）：%s", label, exc)
                    return
                log.warning("  %s 失败：%s（%d/%d，重新打开 lesson 后重试）",
                            label, exc, attempt, attempts)
                await self._open_lesson(lesson)

    # ------------------------------------------------------------ 正文落盘
    async def _write_chapter(self, lesson_dir: Path, number: int, title: str,
                             content: dict, chapter_url: str, breadcrumb: list[str]) -> tuple[Path, int]:
        html_body = content.get("html") or ""
        text = re.sub(r"\s+", " ", content.get("text") or "").strip()
        images = 0

        if html_body and self.settings.images:
            html_body, images, failed = await localize_images(
                html_body, self.session.context.request, lesson_dir / "_assets", chapter_url)
            self.stats.images += images
            if failed:
                log.debug("  %s 有 %d 张图片下载失败（保留远程地址）", title, failed)

        if len(text) < 3:
            filename = f"{number:02d}_EMPTY.html"
            html_body = "<p>EMPTY</p>"
            candidate = lesson_dir / filename
            if candidate.exists() and normalize_title(LocalIndex.title_of(candidate)) != normalize_title(title):
                candidate = lesson_dir / f"{number:02d}_EMPTY ({sanitize(title, 'chapter')}).html"
        else:
            candidate = lesson_dir / f"{number:02d}_{sanitize(title, 'chapter')}.html"

        candidate.write_text(render_chapter(title, html_body, chapter_url, breadcrumb), encoding="utf-8")
        return candidate, images

    # ------------------------------------------------------------ 页面交互
    async def _open_lesson(self, lesson: catalog.Lesson) -> list[dict]:
        page = self.page
        self._lesson_title = lesson.title
        await page.goto(lesson.url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        try:
            await page.wait_for_selector("#lesson-panel", timeout=RENDER_TIMEOUT)
        except PWTimeout:
            log.warning("  lesson 页面渲染超时：%s", lesson.title)
            return []
        await dismiss_modals(page)
        await self._ensure_nav_panel_open()
        chapters = await self._read_chapters()
        if not chapters:
            await self._ensure_chapter_group_open(force=True)
            chapters = await self._read_chapters()
        return chapters

    async def _read_chapters(self) -> list[dict]:
        items: list[dict] = []
        for _ in range(3):
            items = await self.page.evaluate(_READ_CHAPTERS_JS, _CHAPTER_ITEMS)
            if items:
                return items
            await self.page.wait_for_timeout(1000)
        return items

    async def _ensure_nav_panel_open(self) -> None:
        toggle = self.page.locator('button[aria-controls="lesson-panel"]')
        if await toggle.count():
            try:
                if await toggle.first.get_attribute("aria-expanded") == "false":
                    await safe_click(toggle.first, self.page, timeout=10_000)
                    await self.page.wait_for_timeout(600)
            except Exception:
                pass

    async def _ensure_chapter_group_open(self, force: bool = False) -> None:
        """chapter 列表可能折叠：优先展开标题匹配当前 lesson 的分组。"""
        page = self.page
        if not force and await page.locator(_CHAPTER_ITEMS).count():
            return
        buttons = page.locator('#lesson-panel .chakra-accordion__item > button')
        total = await buttons.count()
        if not total:
            return
        wanted = normalize_title(self._lesson_title)
        order = list(range(total))
        if wanted:
            texts = []
            for index in range(total):
                try:
                    texts.append(normalize_title(await buttons.nth(index).inner_text()))
                except Exception:
                    texts.append("")
            order.sort(key=lambda i: 0 if wanted in texts[i] else 1)
        for index in order:
            try:
                if await buttons.nth(index).get_attribute("aria-expanded") == "true":
                    continue
                await safe_click(buttons.nth(index), page, timeout=10_000)
                await page.wait_for_timeout(800)
                if await page.locator(_CHAPTER_ITEMS).count():
                    return
            except Exception:
                continue

    async def _active_index(self) -> int:
        return await self.page.evaluate(_ACTIVE_INDEX_JS, _CHAPTER_ITEMS)

    async def _activate_chapter(self, position: int) -> None:
        page = self.page
        await dismiss_modals(page)
        if await self._active_index() == position:
            await self._wait_content()
            return

        await self._ensure_chapter_group_open()
        item = page.locator(_CHAPTER_ITEMS).nth(position)
        await item.scroll_into_view_if_needed(timeout=15_000)
        previous_key = await page.evaluate(_CONCEPT_KEY_JS)
        await safe_click(item.locator("button").first, page)

        # 以「该 li 变为 data-active」为主信号，conceptKey 变化作为兜底，避免盲等固定时长
        try:
            await page.wait_for_function(
                "(args) => [...document.querySelectorAll(args[0])]"
                ".findIndex(li => li.getAttribute('data-active') === 'true') === args[1]",
                arg=[_CHAPTER_ITEMS, position], timeout=CONTENT_TIMEOUT * 2)
        except PWTimeout:
            try:
                await page.wait_for_function(
                    "prev => (new URLSearchParams(location.search).get('conceptKey') || '') !== prev",
                    arg=previous_key, timeout=10_000)
            except PWTimeout:
                log.debug("  chapter %d 激活信号超时，继续尝试提取", position + 1)
        await self._wait_content()

    async def _wait_content(self) -> None:
        try:
            await self.page.wait_for_function(_CONTENT_READY_JS, timeout=CONTENT_TIMEOUT)
        except PWTimeout:
            pass
        await self.page.wait_for_timeout(SETTLE_MS)