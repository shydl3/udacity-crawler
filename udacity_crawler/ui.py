"""终端交互层：空格多选菜单、表格与人性化格式化。"""
from __future__ import annotations

import os
import select
import sys

IS_WINDOWS = os.name == "nt"

_CURSOR_UP = "\x1b[{}A"
_CLEAR_LINE = "\x1b[2K"
_HIDE_CURSOR = "\x1b[?25l"
_SHOW_CURSOR = "\x1b[?25h"
_DIM = "\x1b[90m"
_BOLD = "\x1b[1m"
_CYAN = "\x1b[36m"
_GREEN = "\x1b[32m"
_RESET = "\x1b[0m"

_UP_KEYS = {"UP", "k", "K"}
_DOWN_KEYS = {"DOWN", "j", "J"}
_CONFIRM_KEYS = {"\r", "\n"}
_ABORT_KEYS = {"q", "Q", "ESC", "\x03"}


class KeyReader:
    """单键读取（POSIX 用 termios，Windows 用 msvcrt）。"""

    def __init__(self) -> None:
        self._fd = sys.stdin.fileno()
        self._restore = None
        self._msvcrt = None

    def __enter__(self) -> "KeyReader":
        if IS_WINDOWS:
            import msvcrt

            self._msvcrt = msvcrt
        else:
            import termios
            import tty

            self._restore = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        sys.stdout.write(_HIDE_CURSOR)
        sys.stdout.flush()
        return self

    def __exit__(self, *exc_info) -> None:
        if IS_WINDOWS:
            pass
        else:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._restore)
        sys.stdout.write(_SHOW_CURSOR)
        sys.stdout.flush()

    def read(self) -> str:
        if IS_WINDOWS:
            char = self._msvcrt.getwch()
            if char in ("\x00", "\xe0"):
                return {"H": "UP", "P": "DOWN"}.get(self._msvcrt.getwch(), "")
            return char
        char = os.read(self._fd, 1).decode("utf-8", "ignore")
        if char != "\x1b":
            return char
        # 方向键是 ESC + [A/[B 这样的转义序列，用 select 收齐剩余字节
        buf = ""
        while len(buf) < 4 and select.select([self._fd], [], [], 0.02)[0]:
            buf += os.read(self._fd, 1).decode("utf-8", "ignore")
        return {
            "[A": "UP", "[B": "DOWN", "OA": "UP", "OB": "DOWN",
            "[H": "HOME", "[F": "END", "[1~": "HOME", "[4~": "END",
        }.get(buf, "ESC")


class _Renderer:
    """原地重绘：避免整屏清空导致闪烁。"""

    def __init__(self) -> None:
        self._lines = 0

    def draw(self, lines: list[str]) -> None:
        buf = []
        if self._lines:
            buf.append(_CURSOR_UP.format(self._lines))
        for line in lines:
            buf.append(_CLEAR_LINE + line + "\n")
        buf.append("\x1b[J")
        sys.stdout.write("".join(buf))
        sys.stdout.flush()
        self._lines = len(lines)

    def finish(self) -> None:
        self._lines = 0


def ellipsis(text: str, width: int) -> str:
    if width <= 1:
        return text[:width]
    return text if len(text) <= width else text[: width - 1] + "…"


def format_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def format_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.0f} {unit}" if unit in ("B", "KB") else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


def _fallback_select(labels: list[str]) -> list[int]:
    """非 TTY（管道/CI）时的降级输入。"""
    for idx, label in enumerate(labels, 1):
        print(f"  {idx:>2}. {label}")
    try:
        raw = input("输入要下载的编号（逗号分隔，all 表示全部，直接回车取消）: ").strip()
    except EOFError:
        return []
    if not raw or raw.lower() in {"q", "quit", "exit"}:
        return []
    if raw.lower() in {"a", "all", "*"}:
        return list(range(len(labels)))
    picked: list[int] = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        try:
            index = int(part) - 1
        except ValueError:
            continue
        if 0 <= index < len(labels) and index not in picked:
            picked.append(index)
    return picked


def multiselect(
    labels: list[str],
    notes: list[str] | None = None,
    title: str = "选择要下载的内容",
    window: int = 12,
) -> list[int]:
    """空格多选：↑/↓ 移动，空格切换，a 全选，n 清空，Enter 开始，q 退出。"""
    if not labels:
        return []
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return _fallback_select(labels)

    notes = notes or [""] * len(labels)
    total = len(labels)
    cursor = 0
    offset = 0
    selected: set[int] = set()
    renderer = _Renderer()

    print(f"{_BOLD}{title}{_RESET}")
    print(f"{_DIM}  ↑/↓ 或 j/k 移动    空格 选择/取消    a 全选    n 清空    "
          f"Enter 开始下载    q 退出{_RESET}")

    def draw() -> None:
        nonlocal offset
        window_size = min(window, total)
        if cursor < offset:
            offset = cursor
        elif cursor >= offset + window_size:
            offset = cursor - window_size + 1
        lines = []
        for index in range(offset, offset + window_size):
            pointer = f"{_CYAN}❯{_RESET}" if index == cursor else " "
            mark = f"{_GREEN}[x]{_RESET}" if index in selected else "[ ]"
            note = f"  {_DIM}{ellipsis(notes[index], 40)}{_RESET}" if notes[index] else ""
            label = ellipsis(labels[index], 62)
            if index == cursor:
                label = f"{_BOLD}{label}{_RESET}"
            lines.append(f" {pointer} {mark} {label}{note}")
        if total > window_size:
            lines.append(f"{_DIM}  … {offset + 1}-{offset + window_size} / {total}{_RESET}")
        lines.append(f"{_DIM}  已选 {len(selected)} 项{_RESET}")
        renderer.draw(lines)

    with KeyReader() as keys:
        while True:
            draw()
            key = keys.read()
            if key in _UP_KEYS:
                cursor = (cursor - 1) % total
            elif key in _DOWN_KEYS:
                cursor = (cursor + 1) % total
            elif key == " ":
                selected.symmetric_difference_update({cursor})
            elif key in {"a", "A"}:
                selected = set(range(total))
            elif key in {"n", "N", "x", "X"}:
                selected.clear()
            elif key in _CONFIRM_KEYS:
                if selected:
                    break
            elif key in _ABORT_KEYS:
                selected.clear()
                break
            elif key.isdigit() and key != "0":
                index = int(key) - 1
                if index < total:
                    cursor = index
                    selected.symmetric_difference_update({index})

    renderer.finish()
    print()
    if selected:
        print(f"{_GREEN}已选择 {len(selected)} 项：{_RESET}")
        for index in sorted(selected):
            print(f"  · {labels[index]}")
    else:
        print(f"{_DIM}未选择任何内容{_RESET}")
    return sorted(selected)


def print_table(rows: list[tuple[str, str]], headers: tuple[str, str] = ("KEY", "TITLE")) -> None:
    if not rows:
        return
    key_width = max(len(headers[0]), *(len(row[0]) for row in rows))
    print(f"{_BOLD}{headers[0].ljust(key_width)}  {headers[1]}{_RESET}")
    for key, title in rows:
        print(f"{_CYAN}{key.ljust(key_width)}{_RESET}  {title}")