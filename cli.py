#!/usr/bin/env python3
"""命令行入口：登录 → 选择 enrollment → 并发下载。

在项目根目录直接运行：python cli.py（或 .venv/bin/python cli.py）
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

try:
    import catalog
    import ui
    from browser import BrowserSession
    from config import (
        DEFAULT_HEADLESS,
        DEFAULT_LOGIN,
        DEFAULT_OUT,
        DEFAULT_PROFILE,
        Settings,
        __version__,
        load_credentials,
        normalize_title,
    )
    from crawler import Crawler
    from logs import log, setup_logging
    from state import LocalIndex, StateStore
except ModuleNotFoundError as exc:  # 未装依赖时给出明确提示，而不是一堆堆栈
    if (exc.name or "").split(".")[0] == "playwright":
        raise SystemExit("缺少依赖 playwright，请先执行：pip install -r requirements.txt") from exc
    raise

_EPILOG = """示例:
  python cli.py                        # 默认：网页 + 图片 + notebook（不含视频/字幕）
  python cli.py --video                # 额外下载 Videos + Transcripts zip
  python cli.py --all                  # 下载全部 enrollment
  python cli.py --select nd787,nd898   # 按 key 或标题直接下载
  python cli.py --list                 # 只列出可下载的 enrollment
  python cli.py --only-lesson <key>    # 补跑某个 lesson
  python cli.py --force                # 忽略已存在，重新抓取正文/图片
  python cli.py --no-images            # 只抓文字，不下载正文图片

默认下载范围: 章节网页（含图片）+ exercise 的 jupyter notebook；视频/字幕不下载。
跳过策略: 逐级按标题比对保存路径，chapter 以 HTML 的 <title> 为指纹，
已存在即跳过（不比对内容），因此可随时中断、重跑续传。
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="udacity-crawler",
        description="Udacity 课程命令行下载器（asyncio + Playwright）",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="保存根目录（默认 ./downloads）")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="浏览器持久化配置目录")
    parser.add_argument("--login-file", type=Path, default=DEFAULT_LOGIN, help="凭据文件（首行邮箱、次行密码）")
    parser.add_argument("--email", default="", help="登录邮箱（覆盖 login.txt）")
    parser.add_argument("--password", default="", help="登录密码（覆盖 login.txt）")
    parser.add_argument("--headless", dest="headless", action="store_true", default=None,
                        help="无窗口运行（默认，见 config.DEFAULT_HEADLESS）")
    parser.add_argument("--headed", dest="headless", action="store_false",
                        help="显示浏览器窗口（首次登录或排查问题时用）")
    parser.add_argument("--channel", default="chrome", help="浏览器内核，默认 chrome，失败回退 chromium")

    parser.add_argument("--select", default="", help="直接指定 enrollment（key 或标题，逗号分隔）")
    parser.add_argument("--all", action="store_true", help="下载全部 enrollment，跳过交互选择")
    parser.add_argument("--list", action="store_true", help="只列出可下载的 enrollment 后退出")

    parser.add_argument("--video", action="store_true",
                        help="额外下载 Videos + Transcripts zip（默认只抓网页/图片/notebook）")
    parser.add_argument("--no-notebooks", action="store_true", help="不下载 exercise 的 jupyter notebook")
    parser.add_argument("--no-images", action="store_true", help="不把正文图片下载到本地 _assets")
    parser.add_argument("--no-block-analytics", action="store_true", help="不屏蔽统计脚本")
    parser.add_argument("--sequential", action="store_true", help="串行下载资源/notebook（默认并行，便于排错）")
    parser.add_argument("--force", action="store_true", help="忽略已存在判定，重新抓取正文（用于更新 HTML 模板/图片）")

    parser.add_argument("--retries", type=int, default=2, help="单个 chapter 的最大尝试次数")
    parser.add_argument("--max-lessons", type=int, default=0, help="调试：最多处理 N 个 lesson")
    parser.add_argument("--max-chapters", type=int, default=0, help="调试：每个 lesson 最多 N 个 chapter")
    parser.add_argument("--only-lesson", default="", help="调试：只处理指定 lesson key")
    parser.add_argument("--log-file", type=Path, default=None, help="同时写日志到文件")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return parser


def _local_note(index: LocalIndex, enrollment: catalog.Enrollment) -> str:
    enrollment_dir = index.find_dir(index.root, enrollment.title)
    if not enrollment_dir:
        return f"{enrollment.key} · 无本地文件"
    chapters = index.chapter_count(enrollment_dir)
    notebooks = index.notebook_count(enrollment_dir)
    if not chapters and not notebooks:
        return f"{enrollment.key} · 无本地文件"
    return f"{enrollment.key} · 本地 {chapters} 篇 / {notebooks} notebook"


def _resolve_selection(args, enrollments: list[catalog.Enrollment],
                       index: LocalIndex) -> list[catalog.Enrollment]:
    if args.select:
        tokens = [token.strip() for token in args.select.split(",") if token.strip()]
        picked: list[catalog.Enrollment] = []
        unresolved: list[str] = []
        for token in tokens:
            matched = [item for item in enrollments
                       if item.key == token or normalize_title(token) in normalize_title(item.title)]
            if not matched:
                unresolved.append(token)
            for item in matched:
                if item not in picked:
                    picked.append(item)
        if unresolved:
            log.warning("未匹配到 enrollment：%s", ", ".join(unresolved))
        if not picked:
            log.error("没有匹配到任何 enrollment，当前账号可用的有：")
            ui.print_table([(item.key, item.title) for item in enrollments])
        return picked
    if args.all:
        return enrollments

    labels = [item.title for item in enrollments]
    notes = [_local_note(index, item) for item in enrollments]
    chosen = ui.multiselect(labels, notes, title="选择要下载的 enrollment")
    return [enrollments[i] for i in chosen]


async def _run(args, settings: Settings) -> None:
    credentials = load_credentials(args.login_file, args.email, args.password)
    index = LocalIndex(settings.out)
    state = StateStore(settings.state_file)

    async with BrowserSession(settings) as session:
        page = await session.new_page()
        await session.login(page, credentials)

        try:
            enrollments = await catalog.fetch_enrollments(page)
            catalog.save_cached_enrollments(settings.enrollments_cache, enrollments)
        except Exception as exc:
            enrollments = catalog.load_cached_enrollments(settings.enrollments_cache)
            if not enrollments:
                raise
            log.warning("读取线上 enrollment 失败（%s），回退到本地缓存", exc)

        if args.list:
            ui.print_table([(item.key, item.title) for item in enrollments])
            return

        selected = _resolve_selection(args, enrollments, index)
        if not selected:
            log.info("没有选择任何 enrollment，退出")
            return

        log.info("开始下载 %d 个 enrollment", len(selected))
        crawler = Crawler(session, settings, state, index)
        await crawler.run(selected, page=page)


def _settings_from_args(args) -> Settings:
    """把命令行参数转成 Settings；未指定的开关沿用 config 里的默认值。"""
    return Settings(
        out=args.out.expanduser().resolve(),
        profile=args.profile.expanduser().resolve(),
        headless=DEFAULT_HEADLESS if args.headless is None else args.headless,
        channel=args.channel,
        resources=args.video,
        notebooks=not args.no_notebooks,
        images=not args.no_images,
        block_analytics=not args.no_block_analytics,
        parallel=not args.sequential,
        overwrite=args.force,
        retries=max(1, args.retries),
        max_lessons=max(0, args.max_lessons),
        max_chapters=max(0, args.max_chapters),
        only_lesson=args.only_lesson,
        log_file=args.log_file,
        verbose=args.verbose,
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = _settings_from_args(args)
    settings.prepare()
    setup_logging(settings.verbose, settings.log_file)
    log.info("保存目录：%s", settings.out)
    log.info("浏览器模式：%s", "headless（无窗口）" if settings.headless else "有界面")
    log.info("下载范围：网页 + 图片 + notebook%s",
             " + 视频/字幕" if settings.resources else "（视频/字幕默认不下载，需要时加 --video）")

    try:
        asyncio.run(_run(args, settings))
    except KeyboardInterrupt:
        log.warning("已手动中断，已完成的文件会保留，下次运行自动跳过")
        return 130
    except SystemExit:
        raise
    except Exception as exc:
        log.error("运行失败：%s", exc)
        if settings.verbose:
            raise
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())