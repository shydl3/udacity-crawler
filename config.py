"""全局配置：路径、超时、标题归一化与凭据读取。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# 所有模块都在项目根目录，直接 python cli.py 运行
BASE_DIR = Path(__file__).resolve().parent

__version__ = "2.0.0"

DEFAULT_OUT = BASE_DIR / "downloads"
DEFAULT_PROFILE = BASE_DIR / "chrome_profile"
DEFAULT_LOGIN = BASE_DIR / "login.txt"

DASHBOARD_URL = "https://www.udacity.com/dashboard/enrollments"
ENROLLMENT_URL = "https://www.udacity.com/enrollment/{key}"

# 模板版本：改动正文清洗/公式处理时递增，旧页面会被自动重抓
HTML_TEMPLATE_VERSION = "4"

# 各阶段等待上限（毫秒）。网络慢时可整体放大，避免写死 sleep。
NAV_TIMEOUT = 120_000
UI_TIMEOUT = 60_000
RENDER_TIMEOUT = 180_000
CONTENT_TIMEOUT = 30_000
# 大文件下载可环境变量调整（毫秒），默认 15 分钟
DOWNLOAD_TIMEOUT = int(os.environ.get("UDACITY_DOWNLOAD_TIMEOUT_MS", "900000"))
WORKSPACE_TIMEOUT = 480_000
SETTLE_MS = 600

# 默认不弹窗；需要看界面时用 --headed
DEFAULT_HEADLESS = True

_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def sanitize(name: str, fallback: str = "untitled", limit: int = 120) -> str:
    """把标题变成安全的文件/目录名（保留可读性，不哈希）。"""
    name = (name or "").replace("\u00a0", " ")
    name = _UNSAFE_CHARS.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    return name[:limit].strip() or fallback


def normalize_title(title: str) -> str:
    """标题归一化：跳过判定只比标题，不做内容 diff。"""
    text = (title or "").replace("\u00a0", " ").casefold()
    text = re.sub(r"[^\w\u3400-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Credentials:
    email: str
    password: str


def load_credentials(path: Path = DEFAULT_LOGIN, email: str = "", password: str = "") -> Credentials:
    """凭据来源优先级：命令行 > 环境变量 > login.txt（首行邮箱、其后密码）。"""
    email = email or os.getenv("UDACITY_EMAIL", "")
    password = password or os.getenv("UDACITY_PASSWORD", "")
    if email and password:
        return Credentials(email, password)

    lines = []
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                lines.append(line)
    if not lines:
        raise SystemExit(f"缺少登录凭据：请填写 {path}（首行邮箱，次行密码）或使用 --email/--password")

    fields = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() in {"email", "password", "user", "pass"}:
            fields[key.strip().lower()] = value.strip()
    email = email or fields.get("email") or fields.get("user") or lines[0]
    password = password or fields.get("password") or fields.get("pass") or (lines[1] if len(lines) > 1 else "")
    if not email or not password:
        raise SystemExit(f"登录凭据不完整：{path}")
    return Credentials(email, password)


@dataclass
class Settings:
    """一次运行的全部可选参数。"""

    out: Path = DEFAULT_OUT
    profile: Path = DEFAULT_PROFILE
    headless: bool = DEFAULT_HEADLESS
    channel: str = "chrome"
    resources: bool = False     # 默认不下载 Videos/Transcripts zip；需要时用 --video
    notebooks: bool = True      # 下载 exercise 的 jupyter notebook
    images: bool = True         # 把正文图片抓到本地 _assets 目录
    block_analytics: bool = True
    parallel: bool = True       # 资源/notebook 用独立 page 并行下载
    overwrite: bool = False     # 忽略已存在判定，重新抓取正文
    retries: int = 2
    max_lessons: int = 0
    max_chapters: int = 0
    only_lesson: str = ""
    log_file: Path | None = None
    verbose: bool = False

    @property
    def state_file(self) -> Path:
        return self.out / ".udacity_state.json"

    @property
    def enrollments_cache(self) -> Path:
        return self.out / ".enrollments.json"

    @property
    def assets_root(self) -> Path:
        return self.out / ".partial"

    def prepare(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        self.assets_root.mkdir(parents=True, exist_ok=True)