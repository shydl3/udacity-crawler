"""统一的日志出口：控制台 + 可选日志文件。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger("udacity")


def setup_logging(verbose: bool = False, log_file: Path | None = None) -> logging.Logger:
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    log.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
    log.addHandler(console)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        log.addHandler(handler)
    return log