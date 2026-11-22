"""落盘状态与本地索引。

跳过判定完全基于「标题」：目录按标题匹配，chapter 以 HTML 里的 <title> 匹配，
不比对文件内容，因此重跑、章节顺序变化、或旧版本脚本产出的文件都能识别。
"""
from __future__ import annotations

import html as html_lib
import json
import os
import re
from pathlib import Path

from config import HTML_TEMPLATE_VERSION, normalize_title, sanitize

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_INDEX_PREFIX_RE = re.compile(r"^\d+_")
_RESOURCE_KEYWORDS = {
    "videos": ("video",),
    # Udacity 的字幕包叫 Subtitles.zip，单词也要算进来
    "transcripts": ("transcript", "subtitle", "caption"),
}
_ARCHIVE_SUFFIXES = {".zip", ".tar", ".gz", ".tgz"}


class StateStore:
    """`<out>/.udacity_state.json`：记录已完成的 lesson 资源，辅助文件系统判定。"""

    VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data.setdefault("version", self.VERSION)
        data.setdefault("enrollments", {})
        data.setdefault("lessons", {})
        return data

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def lesson(self, key: str) -> dict:
        return self.data["lessons"].setdefault(key, {})

    def mark(self, key: str, field: str, value) -> None:
        entry = self.lesson(key)
        bucket = entry.setdefault(field, [])
        if isinstance(value, str):
            if value not in bucket:
                bucket.append(value)
        elif isinstance(value, list):
            bucket.extend(item for item in value if item not in bucket)
        self.save()

    def record_enrollment(self, key: str, title: str, lessons: int) -> None:
        self.data["enrollments"][key] = {"title": title, "lessons": lessons}
        self.save()


class LocalIndex:
    """扫描保存路径，回答「这个标题是否已经下载过」。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    # ----------------------------------------------------------------- 目录
    @staticmethod
    def find_dir(parent: Path, title: str) -> Path | None:
        """在 parent 下按归一化标题查找既有目录（兼容旧版本命名差异）。"""
        if not parent.is_dir():
            return None
        want = normalize_title(title)
        if not want:
            return None
        for child in sorted(parent.iterdir()):
            if child.is_dir() and normalize_title(child.name) == want:
                return child
        return None

    def course_dir(self, enrollment_dir: Path, course_title: str, create: bool = True) -> Path:
        found = self.find_dir(enrollment_dir, course_title)
        if found:
            return found
        path = enrollment_dir / sanitize(course_title, "course")
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def lesson_dir(self, course_dir: Path, lesson_title: str, create: bool = True) -> Path:
        found = self.find_dir(course_dir, lesson_title)
        if found:
            return found
        path = course_dir / sanitize(lesson_title, "lesson")
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    # --------------------------------------------------------------- chapter
    @staticmethod
    def title_of(path: Path) -> str:
        """读 HTML 的 <title>，用作 chapter 的标题指纹。"""
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                head = handle.read(4096)
        except OSError:
            return ""
        match = _TITLE_RE.search(head)
        return html_lib.unescape(match.group(1)).strip() if match else ""

    @staticmethod
    def _title_from_name(name: str) -> str:
        stem = _INDEX_PREFIX_RE.sub("", name)
        if stem.lower().endswith(".html"):
            stem = stem[:-5]
        return "" if stem.strip().upper() == "EMPTY" else stem.strip()

    def chapters(self, lesson_dir: Path) -> dict[str, Path]:
        """返回 归一化标题 -> 文件；标题取自 HTML <title>，退化到文件名。"""
        index: dict[str, Path] = {}
        if not lesson_dir.is_dir():
            return index
        for path in sorted(lesson_dir.glob("*.html")):
            title = self.title_of(path) or self._title_from_name(path.name)
            key = normalize_title(title)
            if key:
                index.setdefault(key, path)
        return index

    def chapter_file(self, lesson_dir: Path, chapter_title: str) -> Path | None:
        return self.chapters(lesson_dir).get(normalize_title(chapter_title))

    @staticmethod
    def math_html_stale(path: Path) -> bool:
        """含公式的旧页面是否需要重抓。

        正文模板会写入 <meta name="udacity-crawler-template" content="N">，
        旧版（无标记或版本号更低）保存的公式页在清洗时丢失了公式子节点，需重新抓取。
        不含公式的页面不受影响，避免无意义重抓。
        """
        try:
            if path.stat().st_size > 4 * 1024 * 1024:
                return False
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False
        if 'class="katex' not in text and "<mjx-container" not in text:
            return False
        marker = f'name="udacity-crawler-template" content="{HTML_TEMPLATE_VERSION}"'
        return marker not in text

    # -------------------------------------------------------------- resource
    def resources(self, lesson_dir: Path) -> dict[str, list[Path]]:
        found: dict[str, list[Path]] = {kind: [] for kind in _RESOURCE_KEYWORDS}
        if lesson_dir.is_dir():
            for path in sorted(lesson_dir.iterdir()):
                if not path.is_file() or path.suffix.lower() not in _ARCHIVE_SUFFIXES:
                    continue
                name = path.name.casefold()
                for kind, keywords in _RESOURCE_KEYWORDS.items():
                    if any(word in name for word in keywords):
                        found[kind].append(path)
        return found

    def has_all_resources(self, lesson_dir: Path) -> bool:
        return all(self.resources(lesson_dir).values())

    # -------------------------------------------------------------- notebook
    def notebook_dir(self, lesson_dir: Path, chapter_title: str) -> Path:
        parent = lesson_dir / "jupyter-notebook"
        return self.find_dir(parent, chapter_title) or parent / sanitize(chapter_title, "chapter")

    def notebook_ready(self, lesson_dir: Path, chapter_title: str) -> bool:
        return any(self.notebook_dir(lesson_dir, chapter_title).glob("*.ipynb"))

    # ----------------------------------------------------------------- 统计
    def chapter_count(self, enrollment_dir: Path) -> int:
        return sum(1 for _ in enrollment_dir.rglob("*.html")) if enrollment_dir.is_dir() else 0

    def notebook_count(self, enrollment_dir: Path) -> int:
        return sum(1 for _ in enrollment_dir.rglob("*.ipynb")) if enrollment_dir.is_dir() else 0