#!/usr/bin/env python3
"""Udacity 课程下载脚本（asyncio + Playwright），按 guide.txt 第 7-11 条实现。

层级：enrollment > course > lesson > chapter
目录：downloads/<enrollment名>/<course名>/<lesson名>/
  - <NN_chapter名>.html        每个 chapter 的文字/图片/代码（忽略嵌入视频），无文本则命名 EMPTY
  - videos zip / transcripts zip   每个 lesson 各下载一次（不解压）
  - jupyter-notebook/<chapter名>/  exercise chapter 的 workspace notebook（下载后解压重命名）

用法：
  .venv/bin/python download_course.py                    # 下载 Flying Car 全部
  .venv/bin/python download_course.py --max-lessons 1    # 调试：只处理第一个 lesson
  .venv/bin/python download_course.py --max-chapters 3   # 调试：每个 lesson 最多 3 个 chapter
"""
import argparse
import asyncio
import re
import shutil
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import unquote

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

BASE = Path(__file__).parent
HTML_DIR = BASE / "html"
PROFILE = BASE / "chrome_profile"
DOWNLOAD_ROOT = BASE / "downloads"
STATE_FILE = HTML_DIR / "download_state.json"

EMAIL = "xxxxx"
PASSWORD = "xxxxx"
ENROLLMENT_ID = "nd787"
ENROLLMENT_NAME = "Flying Car and Autonomous Flight Engineer"

NAV = 120_000
RENDER = 180_000
DL_TIMEOUT = 600_000  # 网络慢，zip 下载放宽到 10 分钟

import json


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def sane(name, fallback="untitled"):
    name = re.sub(r'[\\/:*?"<>|\r\n]+', " ", name or "").strip()
    name = re.sub(r"\s+", " ", name)
    return name[:120] or fallback


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(st):
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


async def dismiss_modals(page):
    await page.evaluate("""() => {
        document.querySelectorAll('.chakra-portal').forEach(p => {
            if (p.querySelector('.chakra-modal__content, .chakra-modal__overlay')) p.remove();
        });
        document.querySelectorAll('.chakra-modal__overlay').forEach(e => e.remove());
        document.body.style.overflow = '';
        document.body.style.pointerEvents = '';
        document.body.removeAttribute('data-scroll-lock');
    }""")


async def safe_click(locator, page, timeout=20_000):
    """点击前清理遮挡弹窗；被拦截时清理后重试一次。"""
    await dismiss_modals(page)
    try:
        await locator.click(timeout=timeout)
    except Exception:
        await dismiss_modals(page)
        await locator.click(timeout=timeout, force=True)


async def ensure_login(page):
    await page.goto("https://www.udacity.com/dashboard/enrollments",
                    wait_until="domcontentloaded", timeout=NAV)
    await page.wait_for_timeout(5000)
    if "auth.udacity.com" not in page.url:
        log("已登录")
        return
    submit = 'form:has(input[name="email"]) button[type="submit"]'
    await page.wait_for_selector('input[name="email"]', timeout=NAV)
    await page.fill('input[name="email"]', EMAIL)
    await page.fill('input[name="password"]', PASSWORD)
    await page.click(submit, no_wait_after=True, timeout=NAV)
    try:
        await page.wait_for_url(lambda u: "auth.udacity.com" not in u, timeout=NAV)
    except PWTimeout:
        pass
    await page.wait_for_timeout(6000)
    if "auth.udacity.com" in page.url:
        raise RuntimeError("登录失败")
    log("登录成功")


async def collect_structure(page):
    """从 enrollment 页 curriculum 提取 course → lesson 层级（标题+URL）。"""
    await page.goto(f"https://www.udacity.com/enrollment/{ENROLLMENT_ID}",
                    wait_until="domcontentloaded", timeout=NAV)
    await page.wait_for_selector('[id^="accordion-button-"]', timeout=RENDER)
    await page.wait_for_timeout(3000)
    data = await page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('[id^="accordion-button-"]').forEach(btn => {
            const panel = document.getElementById(btn.getAttribute('aria-controls'));
            if (!panel) return;
            const links = [...panel.querySelectorAll('a[href*="/lessons/"]')]
                .map(a => ({url: a.href, key: a.href.split('/lessons/')[1].split('?')[0]}))
                .filter(l => l.url.includes('learn.udacity.com'));
            if (!links.length) return;
            // 标题：按钮内第一个 heading（排除描述/进度文本）
            const h = btn.querySelector('h1, h2, h3, h4, h5, h6');
            let title = h ? h.innerText.trim() : '';
            if (!title) {
                const clone = btn.cloneNode(true);
                clone.querySelectorAll('.chakra-progress, svg, p').forEach(e => e.remove());
                title = clone.innerText.trim().split(String.fromCharCode(10))[0];
            }
            out.push({course: title.replace(/\\s+/g, ' '), lessons: links});
        });
        return out;
    }""")
    # lesson 标题稍后从学习页左侧面板补全
    total = sum(len(c["lessons"]) for c in data)
    log(f"结构: {len(data)} 个 course, {total} 个 lesson")
    for c in data:
        log(f"  course: {c['course']!r} ({len(c['lessons'])} lessons)")
    return data


PANEL_CHAPTERS = ('#lesson-panel .chakra-accordion__item:has(> button[aria-expanded="true"]) '
                  '.chakra-accordion__panel ul[aria-label^="Concepts"] > li')


async def open_lesson(page, lesson_url):
    await page.goto(lesson_url, wait_until="domcontentloaded", timeout=NAV)
    await page.wait_for_selector('button:has-text("Next")', timeout=RENDER)
    await dismiss_modals(page)
    await page.wait_for_timeout(3000)
    # course / lesson 标题
    titles = await page.evaluate("""() => {
        const ps = [...document.querySelectorAll('#lesson-panel button[aria-label="Change Course"] p')]
            .map(p => p.innerText.trim());
        const expanded = document.querySelector(
            '#lesson-panel .chakra-accordion__item > button[aria-expanded="true"]');
        let lesson = '';
        if (expanded) {
            const c = expanded.cloneNode(true);
            c.querySelectorAll('.chakra-progress, svg').forEach(e => e.remove());
            lesson = c.innerText.trim();
        }
        return {course: ps[1] || ps[0] || '', lesson};
    }""")
    chapters = await page.eval_on_selector_all(
        PANEL_CHAPTERS,
        "els => els.map(e => e.innerText.trim().split('\\n')[0])")
    return titles, chapters


async def ensure_panel_open(page):
    """确保当前 lesson 的手风琴分组展开（否则 chapter 按钮不可见）。"""
    if await page.locator(PANEL_CHAPTERS + ":visible").count():
        return
    btn = page.locator('#lesson-panel .chakra-accordion__item > button[aria-expanded="false"]')
    for i in range(await btn.count()):
        try:
            await safe_click(btn.nth(i), page, timeout=10_000)
            await page.wait_for_timeout(1500)
            if await page.locator(PANEL_CHAPTERS + ":visible").count():
                return
        except Exception:
            continue


async def goto_chapter(page, idx, prev_key):
    await ensure_panel_open(page)
    li = page.locator(PANEL_CHAPTERS).nth(idx)
    await dismiss_modals(page)
    await li.scroll_into_view_if_needed(timeout=15_000)
    await safe_click(li.locator("button").first, page)
    # 等待 conceptKey 变化（SPA 路由）
    try:
        await page.wait_for_function(
            """prev => {
                const k = new URLSearchParams(location.search).get('conceptKey') || '';
                return k && k !== prev;
            }""", arg=prev_key, timeout=45_000)
    except PWTimeout:
        pass
    await page.wait_for_timeout(4000)
    # 等待内容容器渲染（慢网络下chapter内容异步加载）
    try:
        await page.wait_for_selector(
            '.lesson-content-scroll-container [data-woolf-resource], .lesson-content-scroll-container img, .lesson-content-scroll-container p',
            timeout=30_000)
    except PWTimeout:
        pass
    await dismiss_modals(page)
    return await page.evaluate(
        "() => new URLSearchParams(location.search).get('conceptKey') || ''")


async def extract_content(page):
    """提取 chapter 内容 HTML（guide 第9条的 div.css-s3v1ed 容器，去掉视频）。"""
    return await page.evaluate("""() => {
        const sc = document.querySelector('.lesson-content-scroll-container');
        let c = null;
        if (sc) {
            // 内容容器：跳过 AI Notice 横幅，取包含实际资源的 div
            c = [...sc.children].find(d => d.querySelector('[data-woolf-resource]'))
                || [...sc.children].find(d => !d.querySelector('[role="alert"]') && d.innerText.trim())
                || sc;
        }
        if (!c) return {html: '', text: ''};
        const clone = c.cloneNode(true);
        // 忽略嵌入视频与播放器/按钮等 UI 噪声
        clone.querySelectorAll('iframe, video, object, embed, button, [data-testid="html-player"]').forEach(e => e.remove());
        return {html: clone.innerHTML, text: clone.innerText || ''};
    }""")


def chapter_html(title, body):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
 body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif; max-width: 900px;
        margin: 2em auto; padding: 0 1em; line-height: 1.6; color: #222; }}
 img {{ max-width: 100%; height: auto; }}
 pre, code {{ background: #f5f5f5; border-radius: 4px; }}
 pre {{ padding: 1em; overflow-x: auto; }}
 code {{ padding: 0.15em 0.3em; }}
 pre code {{ padding: 0; }}
 table {{ border-collapse: collapse; }}
 td, th {{ border: 1px solid #ccc; padding: 0.4em 0.6em; }}
</style>
</head>
<body>
<h1>{title}</h1>
{body}
</body>
</html>
"""


async def download_zips(page, lesson_dir, state, lesson_key):
    """右侧 Lesson Resources 面板：Videos / Transcripts zip 各下载一次。"""
    done = state.setdefault("zips", {}).setdefault(lesson_key, [])
    targets = [("Videos", 'button[aria-label^="Download Videos Zip File"]'),
               ("Transcripts", 'button[aria-label^="Download Transcripts Zip File"]')]
    for label, sel in targets:
        if label in done:
            continue
        btn = page.locator(sel).first
        if not await btn.count():
            # 打开资源侧面板
            await page.locator('button[aria-label="Lesson Resources"]').first.click()
            await page.wait_for_timeout(4000)
            btn = page.locator(sel).first
        if not await btn.count():
            log(f"  [zips] 未找到 {label} 下载按钮")
            continue
        try:
            async with page.expect_download(timeout=DL_TIMEOUT) as dl_info:
                await btn.click()
            dl = await dl_info.value
            dest = lesson_dir / unquote(dl.suggested_filename)
            await dl.save_as(dest)
            done.append(label)
            save_state(state)
            log(f"  [zips] {label} -> {dest.name} ({dest.stat().st_size/1e6:.1f} MB)")
        except Exception as e:
            log(f"  [zips] {label} 下载失败: {e}")
    # 关闭侧面板
    close = page.locator('button[aria-label="Close Side Panel"]:visible')
    if await close.count():
        try:
            await close.first.click(timeout=5000)
        except Exception:
            pass


def extract_notebooks(archive: Path, nb_dir: Path):
    """解压 zip/tar.gz，提取所有 .ipynb（排除 checkpoints）到 nb_dir。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        if tarfile.is_tarfile(archive):
            with tarfile.open(archive) as t:
                t.extractall(tmp, filter="data")
        else:
            with zipfile.ZipFile(archive) as z:
                z.extractall(tmp)
        nb_dir.mkdir(parents=True, exist_ok=True)
        found = [f for f in tmp.rglob("*.ipynb") if ".ipynb_checkpoints" not in f.parts]
        for f in found:
            shutil.move(str(f), nb_dir / f.name)
        return len(found)


async def handle_active_workspace_modal(page):
    """处理 Active Workspace 弹窗：优先 shutdown 旧 workspace，然后关闭弹窗。
    返回 True 表示检测到并处理了弹窗。"""
    modal = page.locator('.chakra-modal__content').filter(
        has_text=re.compile(r"active\s*workspace", re.I))
    if not await modal.count():
        return False
    log("  [exercise] 检测到 Active Workspace 弹窗")
    # 借弹窗机会 shutdown 之前的 workspace
    shutdown = modal.locator('button').filter(has_text=re.compile(r"shut\s*down", re.I))
    if await shutdown.count():
        try:
            await shutdown.first.click(timeout=10_000)
            log("  [exercise] 已点击 Shutdown，等待旧 workspace 关闭...")
            await page.wait_for_timeout(10_000)
            # 可能的确认弹窗
            confirm = page.locator('.chakra-modal__content button').filter(
                has_text=re.compile(r"^(yes|confirm|ok|shut\s*down)", re.I))
            if await confirm.count():
                await confirm.first.click(timeout=10_000)
                await page.wait_for_timeout(8000)
        except Exception as e:
            log(f"  [exercise] shutdown 失败: {e}")
    # 关闭弹窗（Close 按钮或 Escape）
    close = modal.locator('button[aria-label*="Close" i]')
    try:
        if await close.count():
            await close.first.click(timeout=5000)
        else:
            await page.keyboard.press("Escape")
    except Exception:
        await dismiss_modals(page)
    await page.wait_for_timeout(2000)
    return True


async def wait_workspace_ready(page, timeout=480_000):
    """等待 Settings 按钮出现（workspace 就绪标志）。"""
    settings = page.locator('.lesson-content-scroll-container button').filter(
        has_text=re.compile(r"^\s*\d*\s*Settings\s*$"))
    try:
        await settings.first.wait_for(state="visible", timeout=timeout)
        return settings
    except PWTimeout:
        return None


async def handle_exercise(page, lesson_dir, chapter_name, state, key):
    """exercise chapter：Start Workspace → Settings → 下载 notebook 压缩包 → 解压重命名。"""
    nb_dir = lesson_dir / "jupyter-notebook" / sane(chapter_name)
    # 按文件名检查：已有 notebook 则跳过
    if nb_dir.exists() and any(nb_dir.glob("*.ipynb")):
        log(f"  [exercise] 已有 notebook，跳过 {nb_dir.name}")
        return
    # 滚到底
    await page.evaluate("""() => {
        const c = document.querySelector('.lesson-content-scroll-container');
        if (c) c.scrollTop = c.scrollHeight;
    }""")
    await page.wait_for_timeout(3000)
    # 定位 Start Workspace 按钮
    launch = page.locator('.lesson-content-scroll-container button').filter(
        has_text=re.compile(r"start\s*workspace", re.I))
    if not await launch.count():
        log("  [exercise] 无 Start Workspace 按钮，跳过")
        return
    try:
        await launch.last.scroll_into_view_if_needed()
        await safe_click(launch.last, page)
    except Exception as e:
        log(f"  [exercise] Start Workspace 点击失败: {e}")
        return
    log("  [exercise] workspace 启动中...")
    await page.wait_for_timeout(6000)
    # 若弹出 Active Workspace 弹窗：shutdown 旧 workspace 后重新启动
    if await handle_active_workspace_modal(page):
        try:
            await safe_click(launch.last, page)
            log("  [exercise] 已重新点击 Start Workspace")
        except Exception as e:
            log(f"  [exercise] 重新启动失败: {e}")
            return
    settings = await wait_workspace_ready(page)
    if settings is None:
        # 启动不成功：再试一次（借弹窗 shutdown 旧 workspace）
        log("  [exercise] 首次启动超时，重试...")
        try:
            await safe_click(launch.last, page)
            await page.wait_for_timeout(6000)
            await handle_active_workspace_modal(page)
            await safe_click(launch.last, page)
        except Exception:
            pass
        settings = await wait_workspace_ready(page)
    if settings is None:
        log("  [exercise] workspace 启动超时，跳过")
        return
    await page.wait_for_timeout(3000)
    await settings.first.click()
    await page.wait_for_timeout(3000)
    # Settings 面板中的下载按钮
    dl_btn = page.locator('.lesson-content-scroll-container button').filter(
        has_text=re.compile(r"download", re.I))
    if not await dl_btn.count():
        await page.screenshot(path=str(HTML_DIR / "exercise_settings_debug.png"))
        log("  [exercise] 未找到下载按钮，已截图 html/exercise_settings_debug.png")
        return
    try:
        async with page.expect_download(timeout=DL_TIMEOUT) as dl_info:
            await safe_click(dl_btn.last, page)
        dl = await dl_info.value
        tmp_archive = lesson_dir / f"_tmp_{unquote(dl.suggested_filename)}"
        await dl.save_as(tmp_archive)
        # 解压（zip 或 tar.gz）并按 chapter 名重命名目录
        count = extract_notebooks(tmp_archive, nb_dir)
        tmp_archive.unlink()
        state.setdefault("notebooks", {})[key] = str(nb_dir.relative_to(BASE))
        save_state(state)
        log(f"  [exercise] notebook x{count} -> {nb_dir.relative_to(BASE)}")
    except Exception as e:
        log(f"  [exercise] notebook 下载失败: {e}")


async def run(args):
    state = load_state()
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(PROFILE), channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1440, "height": 900}, locale="en-US",
            accept_downloads=True)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(60_000)
        page.set_default_navigation_timeout(NAV)
        try:
            await ensure_login(page)
            structure = await collect_structure(page)
            enr_dir = DOWNLOAD_ROOT / sane(ENROLLMENT_NAME)
            lesson_count = 0
            for course in structure:
                for lesson in course["lessons"]:
                    lesson_count += 1
                    if args.only_lesson and lesson["key"] != args.only_lesson:
                        continue
                    if lesson_count < args.start_lesson:
                        continue
                    if args.max_lessons and lesson_count > args.max_lessons:
                        break
                    key = lesson["key"]
                    log(f"=== lesson {lesson_count}: {key} ===")
                    titles, chapters = await open_lesson(page, lesson["url"])
                    if not chapters:
                        # 面板可能折叠，展开后重新收集
                        await ensure_panel_open(page)
                        chapters = await page.eval_on_selector_all(
                            PANEL_CHAPTERS,
                            "els => els.map(e => e.innerText.trim().split('\\n')[0])")
                    course_name = sane(titles["course"] or course["course"])
                    lesson_name = sane(titles["lesson"] or key)
                    lesson_dir = enr_dir / course_name / lesson_name
                    lesson_dir.mkdir(parents=True, exist_ok=True)
                    log(f"  目录: {lesson_dir.relative_to(BASE)} ({len(chapters)} chapters)")
                    # 每 lesson 一次：videos / transcripts zip（默认跳过，--zips 开启）
                    if args.zips:
                        await download_zips(page, lesson_dir, state, key)
                    # 逐 chapter
                    concept_key = await page.evaluate(
                        "() => new URLSearchParams(location.search).get('conceptKey') || ''")
                    n = len(chapters)
                    if args.max_chapters:
                        n = min(n, args.max_chapters)
                    for i in range(n):
                        name = chapters[i] if i < len(chapters) else ""
                        try:
                            # 重新定位（点击后 DOM 可能刷新）
                            if i > 0:
                                concept_key = await goto_chapter(page, i, concept_key)
                            else:
                                await page.wait_for_timeout(2000)
                            # 按文件名检查：已有对应 html 则跳过提取
                            base = f"{i+1:02d}_{sane(name, 'chapter')}"
                            existing = list(lesson_dir.glob(f"{i+1:02d}_*.html"))
                            if existing:
                                log(f"  [ch {i+1}] {name!r} 已存在，跳过 ({existing[0].name})")
                            else:
                                content = await extract_content(page)
                                text = re.sub(r"\s+", " ", content["text"]).strip()
                                if len(text) < 3:
                                    fname = lesson_dir / f"{i+1:02d}_EMPTY.html"
                                    fname.write_text(chapter_html(name or "EMPTY", "<p>EMPTY</p>"),
                                                     encoding="utf-8")
                                    log(f"  [ch {i+1}] {name!r} -> EMPTY")
                                else:
                                    fname = lesson_dir / f"{base}.html"
                                    fname.write_text(chapter_html(name or "chapter", content["html"]),
                                                     encoding="utf-8")
                                    log(f"  [ch {i+1}] {name!r} -> {fname.name} ({len(text)} chars)")
                            # exercise：额外 workspace notebook（内部同样按文件跳过）
                            if re.search(r"exercise", name, re.I):
                                await handle_exercise(page, lesson_dir, name, state,
                                                      f"{key}:{concept_key}")
                        except Exception as e:
                            log(f"  [ch {i+1}] {name!r} 处理失败（继续）: {e}")
                    log(f"  lesson 完成: {lesson_name}")
                if args.max_lessons and lesson_count >= args.max_lessons:
                    break
            log("全部下载任务结束")
        finally:
            save_state(state)
            await ctx.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-lessons", type=int, default=0)
    ap.add_argument("--max-chapters", type=int, default=0)
    ap.add_argument("--start-lesson", type=int, default=1, help="从第 N 个 lesson 开始（全局序号）")
    ap.add_argument("--only-lesson", default="", help="只处理指定 lesson key（补跑用）")
    ap.add_argument("--zips", action="store_true", help="同时下载 videos/transcripts zip（默认不下载）")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()