"""后台下载 worker：把慢操作（zip、workspace notebook）从正文抓取流程里剥离开。

每个 worker 用独立 page + 队列，与主 page 的 chapter 翻页并行执行，
主流程只在队列里投递任务，不再阻塞等待。
"""
from __future__ import annotations

import asyncio
import re
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PWTimeout

from browser import BrowserSession, dismiss_modals, safe_click
from config import (
    DOWNLOAD_TIMEOUT,
    NAV_TIMEOUT,
    RENDER_TIMEOUT,
    SETTLE_MS,
    WORKSPACE_TIMEOUT,
    Settings,
    sanitize,
)
from logs import log
from state import LocalIndex, StateStore

RESOURCE_SELECTORS = {
    "videos": 'button[aria-label^="Download Videos Zip File"]',
    "transcripts": 'button[aria-label^="Download Transcripts Zip File"]',
}
RESOURCE_LABELS = {"videos": "Videos", "transcripts": "Transcripts"}

# exercise 页面上的启动按钮（已启动的 workspace 不会再显示它）
_LAUNCH_RE = re.compile(r"(?:start|resume|open|launch|continue)\s+workspace", re.IGNORECASE)
# 没有启动按钮时只等已有 workspace 面板，不必等满整个启动超时
READY_TIMEOUT_NO_LAUNCH = 30_000

_CONTENT_READY_JS = """() => {
    const scroll = document.querySelector('.lesson-content-scroll-container');
    if (!scroll) return false;
    if (scroll.querySelector('[data-woolf-resource]')) return true;
    return (scroll.innerText || '').trim().length > 0;
}"""


def extract_notebooks(archive: Path, target_dir: Path) -> int:
    """从 zip/tar 包里提取所有 .ipynb（排除 checkpoints）到目标目录。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        if tarfile.is_tarfile(archive):
            with tarfile.open(archive) as handle:
                try:
                    handle.extractall(tmp_dir, filter="data")
                except TypeError:  # Python < 3.12
                    handle.extractall(tmp_dir)
        else:
            with zipfile.ZipFile(archive) as handle:
                handle.extractall(tmp_dir)
        found = [path for path in tmp_dir.rglob("*.ipynb") if ".ipynb_checkpoints" not in path.parts]
        for path in found:
            shutil.move(str(path), target_dir / path.name)
    return len(found)


async def _save_download(download, target_dir: Path) -> Path:
    # Udacity 的文件名用 + 代替空格，且可能带 %21 之类的转义
    name = sanitize(unquote(download.suggested_filename or "").replace("+", " "), "download.zip")
    dest = target_dir / name
    if dest.exists():
        dest = target_dir / f"{Path(name).stem}-{int(asyncio.get_running_loop().time())}{Path(name).suffix}"
    await download.save_as(dest)
    return dest


class _QueueWorker:
    """队列 + 独立 page 的通用 worker。"""

    name = "worker"

    def __init__(self, session: BrowserSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._page: Page | None = None
        self.pending = 0
        self.failures = 0

    async def _get_page(self) -> Page:
        if self._page is None or self._page.is_closed():
            self._page = await self.session.new_page()
        return self._page

    async def _discard_page(self) -> None:
        if self._page and not self._page.is_closed():
            try:
                await self._page.close()
            except Exception:
                pass
        self._page = None

    async def start(self) -> None:
        await self._get_page()
        self._task = asyncio.create_task(self._loop(), name=f"udacity-{self.name}")

    async def submit(self, job) -> None:
        self.pending += 1
        self.queue.put_nowait(job)

    async def close(self) -> None:
        if not self._task:
            return
        self.queue.put_nowait(None)
        try:
            await self._task
        except Exception as exc:
            log.warning("%s 退出异常：%s", self.name, exc)
        await self._discard_page()

    async def drain(self) -> None:
        """等待队列内已投递的任务全部完成（串行模式下使用）。"""
        await self.queue.join()

    async def _loop(self) -> None:
        while True:
            job = await self.queue.get()
            if job is None:
                self.queue.task_done()
                return
            try:
                await self.process(job)
            except Exception as exc:
                self.failures += 1
                log.warning("[%s] 任务失败：%s", self.name, exc)
                await self._discard_page()
            finally:
                self.pending -= 1
                self.queue.task_done()

    async def process(self, job) -> None:  # pragma: no cover - 子类实现
        raise NotImplementedError


@dataclass
class ResourceJob:
    lesson_url: str
    lesson_dir: Path
    state_key: str
    kinds: list[str]


class ResourceWorker(_QueueWorker):
    """lesson 右侧资源面板：Videos / Transcripts zip，每个 lesson 各一次。"""

    name = "resources"

    def __init__(self, session: BrowserSession, settings: Settings,
                 index: LocalIndex, state: StateStore) -> None:
        super().__init__(session, settings)
        self.index = index
        self.state = state

    async def submit_lesson(self, lesson_url: str, lesson_dir: Path, state_key: str) -> bool:
        """缺什么排队下什么；已经在盘上就直接返回 False。"""
        existing = self.index.resources(lesson_dir)
        kinds = [kind for kind, files in existing.items() if not files]
        if not kinds:
            log.debug("  资源已齐全，跳过 %s", lesson_dir.name)
            return False
        log.info("  [资源] 排队下载 %s 的 %s", lesson_dir.name,
                 "/".join(RESOURCE_LABELS[k] for k in kinds))
        await self.submit(ResourceJob(lesson_url, lesson_dir, state_key, kinds))
        return True

    async def process(self, job: ResourceJob) -> None:
        page = await self._get_page()
        log.debug("  [资源] 打开 lesson 页面：%s", job.lesson_url)
        await page.goto(job.lesson_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        await dismiss_modals(page)
        try:
            await page.wait_for_selector('button[aria-label="Lesson Resources"]', timeout=RENDER_TIMEOUT)
        except PWTimeout:
            log.warning("  [资源] lesson 页面未就绪，跳过 %s", job.lesson_dir.name)
            return

        await self._open_panel(page, job)
        if await page.evaluate("() => /no resources available/i.test(document.body.innerText)"):
            log.info("  [资源] 本 lesson 没有可下载资源：%s", job.lesson_dir.name)
            await self._close_panel(page)
            return

        for kind in job.kinds:
            button = page.locator(RESOURCE_SELECTORS[kind]).first
            if not await button.count():
                log.warning("  [资源] 未找到 %s 下载按钮：%s", RESOURCE_LABELS[kind], job.lesson_dir.name)
                continue
            try:
                log.info("  [资源] 请求 %s，等待服务器打包…", RESOURCE_LABELS[kind])
                async with page.expect_download(timeout=DOWNLOAD_TIMEOUT) as info:
                    await safe_click(button, page)
                download = await info.value
                log.debug("  [资源] 下载已开始：%s", download.suggested_filename)
                dest = await _save_download(download, job.lesson_dir)
                self.state.mark(job.state_key, "resources", kind)
                log.info("  [资源] %s -> %s (%.1f MB)", RESOURCE_LABELS[kind], dest.name,
                         dest.stat().st_size / 1e6)
            except Exception as exc:
                log.warning("  [资源] %s 下载失败：%s", RESOURCE_LABELS[kind], exc)

        await self._close_panel(page)

    @staticmethod
    async def _open_panel(page: Page, job: ResourceJob) -> None:
        """打开右侧资源面板，等任一目标按钮或「无资源」提示出现。"""
        if await page.locator(RESOURCE_SELECTORS[job.kinds[0]]).count():
            return
        trigger = page.locator('button[aria-label="Lesson Resources"]').first
        if not await trigger.count():
            return
        try:
            await safe_click(trigger, page)
            await page.wait_for_function(
                """selectors => selectors.some(s => document.querySelector(s))
                   || /no resources available/i.test(document.body.innerText)""",
                arg=[RESOURCE_SELECTORS[k] for k in job.kinds], timeout=20_000)
        except Exception:
            pass

    @staticmethod
    async def _close_panel(page: Page) -> None:
        close = page.locator('button[aria-label="Close Side Panel"]:visible')
        if await close.count():
            try:
                await close.first.click(timeout=5_000)
            except Exception:
                pass


@dataclass
class NotebookJob:
    chapter_url: str
    lesson_dir: Path
    chapter_title: str
    state_key: str


class WorkspaceWorker(_QueueWorker):
    """exercise chapter：启动（或复用已开启的）workspace → Settings → 下载 notebook 压缩包 → 解压重命名。"""

    name = "notebooks"

    def __init__(self, session: BrowserSession, settings: Settings,
                 index: LocalIndex, state: StateStore) -> None:
        super().__init__(session, settings)
        self.index = index
        self.state = state

    async def submit_chapter(self, chapter_url: str, lesson_dir: Path,
                             chapter_title: str, state_key: str) -> bool:
        if self.index.notebook_ready(lesson_dir, chapter_title):
            log.debug("  [notebook] 已存在，跳过 %s", chapter_title)
            return False
        log.info("  [notebook] 排队下载 %s", chapter_title)
        await self.submit(NotebookJob(chapter_url, lesson_dir, chapter_title, state_key))
        return True

    async def process(self, job: NotebookJob) -> None:
        page = await self._get_page()
        await page.goto(job.chapter_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        try:
            await page.wait_for_function(_CONTENT_READY_JS, timeout=RENDER_TIMEOUT)
        except PWTimeout:
            pass
        await dismiss_modals(page)
        await page.wait_for_timeout(SETTLE_MS)

        await page.evaluate("""() => {
            const scroll = document.querySelector('.lesson-content-scroll-container');
            if (scroll) scroll.scrollTop = scroll.scrollHeight;
        }""")
        await page.wait_for_timeout(1200)

        launch = page.locator('.lesson-content-scroll-container button').filter(has_text=_LAUNCH_RE)
        if await launch.count():
            settings_button = await self._start_workspace(page, launch, job.chapter_title)
        else:
            # 页面不再显示 Start Workspace，通常意味着 workspace 已经处于开启状态；
            # 不阻塞流程，直接去定位 Settings 面板。
            log.info("  [notebook] 无 Start Workspace（workspace 可能已开启），直接定位 Settings：%s",
                     job.chapter_title)
            settings_button = await self._wait_ready(page, timeout=READY_TIMEOUT_NO_LAUNCH)

        if settings_button is None:
            log.info("  [notebook] 未出现 Settings，视为该 chapter 无可下载 notebook：%s",
                     job.chapter_title)
            return

        download_button = await self._open_settings(page, settings_button)
        if download_button is None:
            await page.screenshot(path=str(self.settings.out / "_workspace_debug.png"))
            log.warning("  [notebook] 未找到下载按钮，截图见 _workspace_debug.png")
            return

        archive = None
        try:
            async with page.expect_download(timeout=DOWNLOAD_TIMEOUT) as info:
                await safe_click(download_button.last, page)
            download = await info.value
            archive = self.settings.assets_root / unquote(download.suggested_filename or "notebooks.zip")
            await download.save_as(archive)
            target = self.index.notebook_dir(job.lesson_dir, job.chapter_title)
            count = extract_notebooks(archive, target)
            self.state.mark(job.state_key, "notebooks", job.chapter_title)
            log.info("  [notebook] %s x%d -> %s", job.chapter_title, count,
                     target.relative_to(self.settings.out))
        finally:
            if archive and archive.exists():
                archive.unlink()

    async def _open_settings(self, page: Page, settings_button):
        """点开 Settings 并等下载按钮出现。

        workspace 已开启时 Settings 面板可能本来就是展开的，此时首次点击反而会把它收起；
        再点一次即可重新展开，所以这里最多尝试两次，并用 wait_for 代替固定 sleep。
        """
        download_button = page.locator('.lesson-content-scroll-container button:visible').filter(
            has_text=re.compile(r"download", re.I))
        for _ in range(2):
            try:
                await settings_button.click(timeout=15_000)
            except Exception as exc:
                log.warning("  [notebook] Settings 点击失败：%s", exc)
                continue
            try:
                await download_button.first.wait_for(state="visible", timeout=12_000)
                return download_button
            except PWTimeout:
                continue
        return None

    async def _start_workspace(self, page: Page, launch, chapter_title: str):
        """点击 Start/Resume Workspace 并等 Settings 出现；失败返回 None。"""
        try:
            await launch.last.scroll_into_view_if_needed()
            await safe_click(launch.last, page)
        except Exception as exc:
            log.warning("  [notebook] Start Workspace 点击失败：%s", exc)
            return None

        log.info("  [notebook] workspace 启动中：%s", chapter_title)
        if await self._handle_active_workspace(page):
            try:
                await safe_click(launch.last, page)
                log.info("  [notebook] 旧 workspace 已关闭，重新启动")
            except Exception as exc:
                log.warning("  [notebook] 重新启动失败：%s", exc)
                return None

        settings_button = await self._wait_ready(page)
        if settings_button is None:
            log.info("  [notebook] 首次启动超时，重试一次")
            try:
                await safe_click(launch.last, page)
                await self._handle_active_workspace(page)
                await safe_click(launch.last, page)
            except Exception:
                pass
            settings_button = await self._wait_ready(page)
        return settings_button

    async def _wait_ready(self, page: Page, timeout: int = WORKSPACE_TIMEOUT):
        button = page.locator('.lesson-content-scroll-container button').filter(
            has_text=re.compile(r"^\s*\d*\s*Settings\s*$"))
        try:
            await button.first.wait_for(state="visible", timeout=timeout)
            return button.first
        except PWTimeout:
            return None

    @staticmethod
    async def _handle_active_workspace(page: Page) -> bool:
        """若弹出 Active Workspace：先关闭旧 workspace，再关掉弹窗。"""
        modal = page.locator('.chakra-modal__content').filter(
            has_text=re.compile(r"active\s*workspace", re.I))
        if not await modal.count():
            return False
        log.info("  [notebook] 检测到 Active Workspace 弹窗")
        shutdown = modal.locator('button').filter(has_text=re.compile(r"shut\s*down", re.I))
        if await shutdown.count():
            try:
                await shutdown.first.click(timeout=10_000)
                await page.wait_for_timeout(8_000)
                confirm = page.locator('.chakra-modal__content button').filter(
                    has_text=re.compile(r"^(yes|confirm|ok|shut\s*down)", re.I))
                if await confirm.count():
                    await confirm.first.click(timeout=10_000)
                    await page.wait_for_timeout(6_000)
            except Exception as exc:
                log.warning("  [notebook] shutdown 失败：%s", exc)
        close = modal.locator('button[aria-label*="Close" i]')
        try:
            if await close.count():
                await close.first.click(timeout=5_000)
            else:
                await page.keyboard.press("Escape")
        except Exception:
            await dismiss_modals(page)
        await page.wait_for_timeout(1500)
        return True