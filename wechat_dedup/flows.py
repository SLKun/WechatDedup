"""扫描编排流程辅助。

提供扫描编排逻辑，供 app.py 的 @work worker 调用。
UI 交互由 worker 通过 app.call_from_thread 驱动。
"""
from __future__ import annotations

import time
from pathlib import Path

from .db import Database
from .scanner import ProgressCallback, ScanResult, scan


def run_scan(
    db: Database,
    root: Path,
    accounts: list[str] | None,
    on_progress: ProgressCallback,
) -> ScanResult:
    """执行扫描并返回扫描统计。

    Args:
        db: 当前根目录对应的数据库。
        root: 用户传入的根目录。
        accounts: 本次扫描的账号；None 表示全部已发现账号。
        on_progress: 由调用方提供的扫描进度回调。

    Returns:
        本次扫描的文件计数、复用计数和摘要计数。
    """
    last_emit = [0.0]

    def _emit(phase: str, done: int, total: int, current: str) -> None:
        """限制高频扫描进度回调。"""
        now = time.monotonic()
        if now - last_emit[0] < 0.2 and phase != "done" and done < total:
            return
        last_emit[0] = now
        on_progress(phase, done, total, current)

    return scan(root, db, accounts=accounts, on_progress=_emit)
