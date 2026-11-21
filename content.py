"""chapter 正文：DOM 提取清洗、HTML 模板渲染与图片本地化。

提取范围严格限定在中间正文列 `.lesson-content-scroll-container`（右侧 AI 栏不在其中），
并去掉视频播放器、字幕按钮、表单控件等非图文元素。
"""
from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import re
import time
from pathlib import Path

from playwright.async_api import Page

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
 :root {{
   --fg: #1f2328; --muted: #59636e; --bg: #ffffff; --panel: #f6f8fa;
   --border: #d8dee4; --accent: #0969da; --code: #0f2b3d;
 }}
 @media (prefers-color-scheme: dark) {{
   :root {{ --fg: #e6edf3; --muted: #9198a1; --bg: #0d1117; --panel: #161b22;
            --border: #30363d; --accent: #4493f8; --code: #e6edf3; }}
 }}
 * {{ box-sizing: border-box; }}
 html {{ scroll-behavior: smooth; -webkit-text-size-adjust: 100%; }}
 body {{ margin: 0; background: var(--bg); color: var(--fg);
        font: 16px/1.7 -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }}
 .wrap {{ max-width: 880px; margin: 0 auto; padding: 40px 24px 96px; }}
 .crumbs {{ font-size: 13px; color: var(--muted); margin-bottom: 10px; }}
 .crumbs span + span::before {{ content: " / "; color: var(--border); }}
 h1 {{ font-size: 2rem; line-height: 1.25; letter-spacing: -.01em; margin: 0 0 8px; }}
 .meta {{ color: var(--muted); font-size: 13px; margin-bottom: 26px; }}
 .meta a {{ color: inherit; }}
 .toc {{ background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
        padding: 12px 18px; margin: 0 0 30px; font-size: 14px; }}
 .toc summary {{ cursor: pointer; font-weight: 600; }}
 .toc ol {{ margin: 8px 0 2px; padding-left: 20px; }}
 .toc li.lvl3 {{ margin-left: 16px; }}
 .toc a {{ color: var(--fg); text-decoration: none; }}
 .toc a:hover {{ color: var(--accent); text-decoration: underline; }}
 main > :first-child {{ margin-top: 0; }}
 main p {{ margin: 0 0 1em; }}
 main h2, main h3, main h4 {{ line-height: 1.3; margin: 1.8em 0 .6em; letter-spacing: -.005em; }}
 main h2 {{ font-size: 1.42em; border-bottom: 1px solid var(--border); padding-bottom: .3em; }}
 main h3 {{ font-size: 1.18em; }}
 main ul, main ol {{ padding-left: 1.5em; margin: 0 0 1em; }}
 main li {{ margin: .3em 0; }}
 main img {{ max-width: 100%; height: auto; border-radius: 8px; border: 1px solid var(--border); }}
 main figure {{ margin: 1.2em 0; }}
 main figcaption {{ font-size: 13px; color: var(--muted); margin-top: 6px; }}
 main a {{ color: var(--accent); }}
 main hr {{ border: 0; border-top: 1px solid var(--border); margin: 2em 0; }}
 main blockquote {{ margin: 1.2em 0; padding: .5em 1em; color: var(--muted);
                   border-left: 3px solid var(--accent); background: var(--panel); border-radius: 0 6px 6px 0; }}
 main pre {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
            padding: 14px 16px; overflow-x: auto; margin: 0 0 1.2em; }}
 main pre code {{ background: none; border: 0; padding: 0; color: var(--code); }}
 main code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
             font-size: .9em; background: var(--panel); border-radius: 4px; padding: .15em .35em; }}
 main table {{ border-collapse: collapse; width: 100%; margin: 0 0 1.2em; font-size: 15px; }}
 main th, main td {{ border: 1px solid var(--border); padding: 8px 10px; text-align: left; vertical-align: top; }}
 main th {{ background: var(--panel); }}
 main label {{ display: block; padding: 9px 13px; margin: 6px 0; border: 1px solid var(--border);
              border-radius: 8px; cursor: default; }}
 main label p {{ margin: 0; }}
 main label span {{ display: block; }}
 main label[data-selected="1"] {{ border-color: var(--accent); font-weight: 600;
   background: color-mix(in srgb, var(--accent) 8%, transparent); }}
 main label[data-selected="1"]::before {{ content: "✓ "; color: var(--accent); }}
 main input {{ display: none; }}
 footer {{ margin-top: 64px; padding-top: 14px; border-top: 1px solid var(--border);
          font-size: 12.5px; color: var(--muted); }}
 @media print {{ .toc {{ display: none; }} .wrap {{ max-width: none; padding: 0; }}
                main a[href]::after {{ content: " (" attr(href) ")"; font-size: .8em; color: #666; }} }}
</style>{katex}
</head>
<body>
<div class="wrap">
<nav class="crumbs">{breadcrumb}</nav>
<h1>{title}</h1>
<div class="meta">保存于 {saved_at} · <a href="{source}">原始页面</a></div>
{toc}
<main>
{body}
</main>
<footer>Udacity 课程存档 · 仅保留图文内容，视频未包含。</footer>
</div>
</body>
</html>
"""

# 滚动正文以触发懒加载，再把 srcset 的结果固化到 src（extract 前调用）
_PREPARE_JS = """async () => {
    const scroll = document.querySelector('.lesson-content-scroll-container');
    if (!scroll) return 0;
    const step = Math.max(400, Math.round(scroll.clientHeight * 0.9));
    for (let y = 0; y < scroll.scrollHeight; y += step) {
        scroll.scrollTop = y;
        await new Promise(r => setTimeout(r, 60));
    }
    scroll.scrollTop = 0;
    const imgs = [...scroll.querySelectorAll('img')];
    imgs.forEach(img => {
        if (!img.getAttribute('src') && img.currentSrc) img.setAttribute('src', img.currentSrc);
        img.setAttribute('loading', 'eager');
    });
    return imgs.length;
}"""

_EXTRACT_JS = """() => {
    const scroll = document.querySelector('.lesson-content-scroll-container');
    if (!scroll) return {html: '', text: '', images: 0};
    // 正文列：跳过 AI Notice 横幅，取真正装内容的容器
    const content = [...scroll.children].find(d => d.querySelector('[data-woolf-resource]'))
        || [...scroll.children].find(d => !d.querySelector('[role="alert"]') && (d.textContent || '').trim())
        || scroll;
    const clone = content.cloneNode(true);

    // 1) 非图文元素：播放器、按钮、表单控件、埋点脚本
    clone.querySelectorAll(
        'script, style, link, iframe, video, audio, source, object, embed, button, input, textarea, select,' +
        '[data-testid="html-player"], [role="alert"], .chakra-skeleton, .chakra-spinner'
    ).forEach(el => el.remove());

    // 2) 代码块压平成 <pre><code>纯文本</code></pre>，丢掉行内高亮样式
    clone.querySelectorAll('pre').forEach(pre => {
        const codeEl = pre.querySelector('code');
        const lang = codeEl ? [...codeEl.classList].find(c => c.startsWith('language-')) || '' : '';
        const text = ((codeEl || pre).textContent || '').replace(/\\n+$/, '');
        pre.textContent = '';
        const code = document.createElement('code');
        if (lang) code.className = lang;
        code.textContent = text;
        pre.appendChild(code);
    });

    // 3) 选择题：保留选项文字，标记正确答案，去掉交互控件
    clone.querySelectorAll('label').forEach(label => {
        const checked = label.hasAttribute('data-checked') || !!label.querySelector('[data-checked]');
        label.setAttribute('data-selected', checked ? '1' : '0');
    });

    // 4) 图片：去掉 srcset/懒加载属性，统一用 src
    clone.querySelectorAll('img').forEach(img => {
        if (!img.getAttribute('src') && img.currentSrc) img.setAttribute('src', img.currentSrc);
        img.removeAttribute('srcset');
        img.removeAttribute('data-srcset');
        img.removeAttribute('data-src');
        img.setAttribute('loading', 'lazy');
        img.setAttribute('decoding', 'async');
    });

    // 5) 视频被移除后留下的空壳（无文字、无图/代码/表格）
    const KEEP = 'img, pre, table, ul, ol, blockquote, hr, math, figure';
    for (let pass = 0; pass < 5; pass++) {
        let removed = 0;
        clone.querySelectorAll('div, section, article, figure').forEach(el => {
            if (el.querySelector(KEEP)) return;
            if ((el.textContent || '').trim()) return;
            el.remove();
            removed++;
        });
        if (!removed) break;
    }

    // 6) 去掉 chakra 的样式/交互属性，让模板 CSS 接管（katex 子树除外）
    const allowed = {
        A: ['href', 'name'], IMG: ['src', 'alt', 'width', 'height'],
        TD: ['colspan', 'rowspan'], TH: ['colspan', 'rowspan'],
        OL: ['start'], LI: ['value'], LABEL: ['data-selected'],
        DETAILS: ['open'], MATH: ['display'], TIME: ['datetime'],
    };
    clone.querySelectorAll('*').forEach(el => {
        if (el.closest('.katex') || el.closest('math')) return;
        const keep = allowed[el.tagName] || [];
        [...el.attributes].forEach(attr => {
            const isLang = el.tagName === 'CODE' && attr.name === 'class' && /language-/.test(attr.value);
            if (!keep.includes(attr.name) && !isLang) el.removeAttribute(attr.name);
        });
    });

    // 7) 相对链接补成绝对地址
    clone.querySelectorAll('a[href]').forEach(a => {
        try {
            const href = a.getAttribute('href');
            if (href && !/^(https?:|mailto:|#)/.test(href)) {
                a.setAttribute('href', new URL(href, location.href).href);
            }
        } catch (e) {}
    });

    // 8) 压平冗余的 div/form 与空 span，让 HTML 干净可读
    clone.querySelectorAll('form').forEach(form => form.replaceWith(...[...form.childNodes]));
    for (let pass = 0; pass < 5; pass++) {
        let changed = 0;
        clone.querySelectorAll('span').forEach(span => {
            if (!(span.textContent || '').trim() && !span.querySelector('img, pre, code, math')) {
                span.remove();
                changed++;
            }
        });
        clone.querySelectorAll('div').forEach(div => {
            const kids = [...div.children];
            const ownText = [...div.childNodes]
                .some(node => node.nodeType === 3 && node.textContent.trim());
            if (kids.length === 1 && !ownText && !['PRE', 'TABLE', 'UL', 'OL'].includes(kids[0].tagName)) {
                div.replaceWith(kids[0]);
                changed++;
            }
        });
        if (!changed) break;
    }

    return {
        html: clone.innerHTML,
        text: clone.textContent || '',
        images: clone.querySelectorAll('img').length,
    };
}"""

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_SRC_ATTR_RE = re.compile(r'\bsrc\s*=\s*"([^"]*)"', re.IGNORECASE)
_LAZY_ATTR_RE = re.compile(r'\b(?:data-src|data-original)\s*=\s*"([^"]*)"', re.IGNORECASE)
_DROP_ATTR_RE = re.compile(
    r'\s(?:(?:data-)?srcset|data-src|data-original|loading|decoding)\s*=\s*"[^"]*"', re.IGNORECASE)
_HEADING_RE = re.compile(r"<h([23])\b[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif"}
_MIME_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/svg+xml": ".svg", "image/avif": ".avif",
}
_KATEX_LINK = ('\n<link rel="stylesheet" '
               'href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">')


def _strip_tags(fragment: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _build_toc(body: str) -> tuple[str, str]:
    """从 h2/h3 生成目录锚点；标题太少则不生成。"""
    headings: list[tuple[str, str, str]] = []

    def replace(match: re.Match) -> str:
        level, inner = match.group(1), match.group(2)
        label = _strip_tags(inner)
        if not label:
            return match.group(0)
        anchor = f"sec-{len(headings) + 1}"
        headings.append((level, anchor, label))
        return f'<h{level} id="{anchor}">{inner}</h{level}>'

    body = _HEADING_RE.sub(replace, body)
    if len(headings) < 2:
        return body, ""
    items = "".join(
        f'<li class="lvl{level}"><a href="#{anchor}">{html_lib.escape(label)}</a></li>'
        for level, anchor, label in headings
    )
    toc = f'\n<details class="toc" open><summary>本页目录</summary><ol>{items}</ol></details>'
    return body, toc


def render_chapter(title: str, body: str, source: str = "",
                   breadcrumb: list[str] | None = None) -> str:
    body, toc = _build_toc(body)
    crumbs = "".join(f"<span>{html_lib.escape(part)}</span>"
                     for part in (breadcrumb or []) if part)
    katex = _KATEX_LINK if ("class=\"katex" in body or "<math" in body) else ""
    return _TEMPLATE.format(
        title=html_lib.escape(title),
        body=body,
        source=html_lib.escape(source, quote=True),
        saved_at=time.strftime("%Y-%m-%d %H:%M"),
        breadcrumb=crumbs or "<span>Udacity</span>",
        toc=toc,
        katex=katex,
    )


async def prepare_page(page: Page) -> None:
    """滚动正文触发懒加载图片，尽量确保图文完整。"""
    try:
        await page.evaluate(_PREPARE_JS)
    except Exception:
        pass


async def extract_content(page: Page) -> dict:
    """取出中间正文列并清洗为纯图文 HTML。"""
    try:
        return await page.evaluate(_EXTRACT_JS)
    except Exception:
        return {"html": "", "text": "", "images": 0}


def _asset_name(url: str, content_type: str = "") -> str:
    suffix = Path(url.split("?")[0]).suffix.lower()
    if suffix not in _IMAGE_EXT:
        suffix = _MIME_EXT.get(content_type.split(";")[0].strip().lower(), ".img")
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + suffix


def _pick_url(tag: str, base_url: str) -> str:
    for pattern in (_LAZY_ATTR_RE, _SRC_ATTR_RE):
        match = pattern.search(tag)
        if match:
            url = html_lib.unescape(match.group(1)).strip()
            if url.startswith(("http://", "https://")):
                return url
            if url.startswith("/") and base_url:
                return base_url.rstrip("/") + url
    return ""


async def localize_images(html_text: str, request, assets_dir: Path,
                          base_url: str, concurrency: int = 8) -> tuple[str, int, int]:
    """并发抓取正文图片到 `_assets/`，返回 (新 HTML, 成功数, 失败数)。"""
    tags = _IMG_TAG_RE.findall(html_text)
    if not tags:
        return html_text, 0, 0

    base_url = base_url or "https://learn.udacity.com"
    urls = {url for tag in tags if (url := _pick_url(tag, base_url))}
    if not urls:
        return html_text, 0, 0

    assets_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def fetch(url: str) -> tuple[str, str]:
        async with semaphore:
            try:
                response = await request.get(url, timeout=60_000)
                if not response.ok:
                    return url, ""
                body = await response.body()
                if not body:
                    return url, ""
                name = _asset_name(url, response.headers.get("content-type", ""))
                target = assets_dir / name
                if not target.exists():
                    await asyncio.to_thread(target.write_bytes, body)
                return url, name
            except Exception:
                return url, ""

    results = await asyncio.gather(*(fetch(url) for url in urls))
    mapping = {url: name for url, name in results if name}

    def rewrite(match: re.Match) -> str:
        tag = match.group(0)
        local = mapping.get(_pick_url(tag, base_url))
        if not local:
            return tag
        tag = _DROP_ATTR_RE.sub("", tag)
        relative = f"_assets/{local}"
        if _SRC_ATTR_RE.search(tag):
            return _SRC_ATTR_RE.sub(f'src="{relative}"', tag, count=1)
        return tag.replace("<img", f'<img src="{relative}"', 1)

    failed = len(urls) - len(mapping)
    return _IMG_TAG_RE.sub(rewrite, html_text), len(mapping), failed