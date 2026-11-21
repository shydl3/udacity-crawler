#!/usr/bin/env python3
"""Udacity 课程下载器入口。

  python download_course.py                 # 交互式多选后下载
  python download_course.py --help          # 全部参数说明
"""
import sys


def _missing_dependency(exc: ModuleNotFoundError) -> int:
    if exc.name and exc.name.split(".")[0] == "playwright":
        print("缺少依赖 playwright。请先执行：\n"
              "  pip install -r requirements.txt\n"
              "  python -m playwright install chromium   # 若本机已装 Chrome 可跳过", file=sys.stderr)
        return 1
    raise exc


def main() -> int:
    try:
        from udacity_crawler.cli import main as cli_main
    except ModuleNotFoundError as exc:
        return _missing_dependency(exc)
    return cli_main()


if __name__ == "__main__":
    sys.exit(main())