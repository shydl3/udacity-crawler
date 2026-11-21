"""课程目录：enrollment 列表与 course → lesson 层级提取。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PWTimeout

from browser import safe_click, wait_until_quiet
from config import (
    DASHBOARD_URL,
    ENROLLMENT_URL,
    NAV_TIMEOUT,
    RENDER_TIMEOUT,
)
from logs import log


@dataclass
class Enrollment:
    key: str
    title: str
    url: str


@dataclass
class Lesson:
    key: str
    title: str
    url: str
    part: str = ""


@dataclass
class Course:
    key: str
    title: str
    lessons: list[Lesson] = field(default_factory=list)


_JS_ENROLLMENTS = """() => {
    const root = document.querySelector('[data-testid="enrollments-list"]') || document;
    const found = new Map();
    root.querySelectorAll('a[href*="/enrollment/"]').forEach(a => {
        const match = (a.getAttribute('href') || '').match(/\\/enrollment\\/([^\\/?#]+)/);
        if (!match) return;
        const title = (a.innerText || a.textContent || '').trim().replace(/\\s+/g, ' ');
        if (!title) return;
        if (!found.has(match[1])) found.set(match[1], {key: match[1], title});
    });
    return [...found.values()];
}"""

_JS_CURRICULUM = """() => {
    const out = [];
    const seenLessons = new Set();
    document.querySelectorAll('article[id]').forEach(art => {
        const btn = art.querySelector('[id^="accordion-button-"]');
        if (!btn) return;
        const panel = document.getElementById(btn.getAttribute('aria-controls') || '');
        if (!panel) return;
        const heading = btn.querySelector('h1,h2,h3,h4,h5,h6');
        let title = heading ? heading.innerText.trim() : '';
        if (!title) {
            const bar = art.querySelector('[role="progressbar"]');
            title = bar ? (bar.getAttribute('aria-label') || '').replace(/^Course \\d+\\s*/, '') : '';
        }
        const lessons = [];
        panel.querySelectorAll('a[href*="/lessons/"]').forEach(a => {
            const raw = (a.getAttribute('href') || '').split('/lessons/')[1] || '';
            const key = raw.split(/[?#]/)[0];
            if (!key || seenLessons.has(key)) return;
            seenLessons.add(key);
            const card = a.closest('article');
            const h = card ? card.querySelector('h1,h2,h3,h4,h5,h6') : null;
            lessons.push({
                key: key,
                title: h ? h.innerText.trim().replace(/\\s+/g, ' ') : '',
                url: a.href.split('?')[0],
            });
        });
        out.push({
            key: art.id || '',
            title: (title || '').replace(/\\s+/g, ' '),
            lessons: lessons,
        });
    });
    return out;
}"""


async def fetch_enrollments(page: Page) -> list[Enrollment]:
    """从 dashboard 读取所有 enrollment（滚动加载直到数量稳定）。"""
    if page.url.split("?")[0].rstrip("/") != DASHBOARD_URL:
        await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    try:
        await page.wait_for_selector('a[href*="/enrollment/"]', timeout=RENDER_TIMEOUT)
        await page.wait_for_function(
            "() => document.querySelectorAll('a[href*=\"/enrollment/\"]').length > 0",
            timeout=20_000)
    except PWTimeout:
        raise RuntimeError("enrollment 列表未渲染：登录态可能已失效，请检查账号")
    await wait_until_quiet(page, 5_000)

    previous = -1
    for _ in range(15):
        count = await page.locator('a[href*="/enrollment/"]').count()
        if count and count == previous:
            break
        previous = count
        await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(600)

    raw = await page.evaluate(_JS_ENROLLMENTS)
    enrollments = [
        Enrollment(key=item["key"], title=item["title"], url=ENROLLMENT_URL.format(key=item["key"]))
        for item in raw
    ]
    if not enrollments:
        raise RuntimeError("没有读取到任何 enrollment")
    return enrollments


def load_cached_enrollments(path: Path) -> list[Enrollment]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [Enrollment(key=item["key"], title=item["title"],
                       url=item.get("url") or ENROLLMENT_URL.format(key=item["key"]))
            for item in raw]


def save_cached_enrollments(path: Path, enrollments: list[Enrollment]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([e.__dict__ for e in enrollments], ensure_ascii=False, indent=2),
                    encoding="utf-8")


async def _expand_all(page: Page) -> None:
    """兜底：面板内容未进 DOM 时，展开全部 course 手风琴。"""
    buttons = page.locator('[id^="accordion-button-"][aria-expanded="false"]')
    for index in range(await buttons.count()):
        try:
            await safe_click(buttons.nth(index), page, timeout=10_000)
            await page.wait_for_timeout(500)
        except Exception:
            continue


async def fetch_curriculum(page: Page, enrollment: Enrollment) -> list[Course]:
    """一次性拿到 enrollment 下所有 course/lesson（lesson 标题也在 DOM 里，省去逐页探测）。"""
    await page.goto(enrollment.url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    try:
        await page.wait_for_selector('[id^="accordion-button-"]', timeout=RENDER_TIMEOUT)
        # 等 lesson 链接进入 DOM，比盲目 networkidle 快很多
        await page.wait_for_function(
            "() => document.querySelectorAll('a[href*=\"/lessons/\"]').length > 0",
            timeout=25_000)
    except PWTimeout:
        raise RuntimeError(f"课程目录渲染超时：{enrollment.title}")
    await wait_until_quiet(page, 5_000)

    data = await page.evaluate(_JS_CURRICULUM)
    if not data or not any(course["lessons"] for course in data):
        await _expand_all(page)
        await page.wait_for_timeout(1500)
        data = await page.evaluate(_JS_CURRICULUM)

    courses: list[Course] = []
    for item in data:
        lessons = [Lesson(key=l["key"], title=l["title"] or l["key"], url=l["url"],
                          part=item["key"])
                   for l in item["lessons"]]
        if lessons:
            courses.append(Course(key=item["key"], title=item["title"] or item["key"], lessons=lessons))
    log.info("目录：%s 个 course / %s 个 lesson", len(courses),
             sum(len(course.lessons) for course in courses))
    return courses