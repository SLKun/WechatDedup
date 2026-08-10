"""目录扫描与增量内容摘要计算。

ADR-0004: mtime+size 未变时复用旧 content_hash，物理文件标识始终刷新。
摘要策略：按 size 预筛，只给 size 出现≥2 次的文件计算内容摘要。
ADR-0007: 内容摘要算法固定用 BLAKE3（大文件提速 ~10×）。

清理临时文件：扫描前删除附件目录中残留的 `.{原名}.dedup-tmp-{uuid}`。
"""
# 依赖库：blake3
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import blake3

from .db import Database
from .models import CurrentFile

# 进度回调签名：(phase, done, total, current_path)
#   phase: "cleaning" 清理 / "walking" 遍历 / "hashing" 计算内容摘要 /
#          "done" 完成
#   cleaning 尚未取得工作量时 total 用 0 表示未知
ProgressCallback = Callable[[str, int, int, str], None]


@dataclass
class ScanResult:
    """一次扫描的文件计数、复用计数和摘要计数。"""

    scanned: int   # 扫到的文件总数
    new: int       # 新入库的
    reused: int    # 大小和修改时间未变，复用旧 hash 的
    hashed: int    # 实际读文件算 hash 的次数


def discover_accounts(root: Path) -> list[str]:
    """发现账号目录。

    一个「账号目录」= root 下含 FileStorage\\File\\ 的子目录（真实结构见 CONTEXT）。
    过滤掉非账号杂目录（根下的 readme、缓存等）。账号目录名前缀不固定（wxid_/ssi_/…）。
    """
    if not root.exists():
        return []
    accounts = []
    for p in root.iterdir():
        if p.is_dir() and (p / "FileStorage" / "File").is_dir():
            accounts.append(p.name)
    return sorted(accounts)


def cleanup_tmp_files(root: Path, accounts: list[str] | None = None) -> int:
    """清理附件目录中残留的去重临时文件。

    临时文件命名形如 `.{原名}.dedup-tmp-{uuid}`（见 deduper.create_hardlink），
    固定标记是 `.dedup-tmp-`，故用 `*.dedup-tmp-*` 匹配。
    """
    if accounts is None:
        accounts = discover_accounts(root)
    removed = 0
    for account in accounts:
        file_dir = root / account / "FileStorage" / "File"
        if not file_dir.is_dir():
            continue
        for p in file_dir.rglob("*.dedup-tmp-*"):
            if p.is_file():
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


def compute_hash(path: Path) -> str:
    """计算文件内容的 BLAKE3 摘要（ADR-0007）。"""
    h = blake3.blake3()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _walk_files(root: Path, accounts: list[str]) -> list[tuple[str, Path]]:
    """遍历指定账号的 FileStorage\\File\\ 目录下的文件。"""
    files: list[tuple[str, Path]] = []
    for account in accounts:
        file_dir = root / account / "FileStorage" / "File"
        if file_dir.is_dir():
            files.extend((account, path) for path in file_dir.rglob("*") if path.is_file())
    return files


def _rel_path(root: Path, p: Path) -> str:
    """返回路径相对于微信文件根目录的 POSIX 格式路径。"""
    return p.relative_to(root).as_posix()


def scan(
    root: Path,
    db: Database,
    accounts: list[str] | None = None,
    on_progress: ProgressCallback | None = None,
) -> ScanResult:
    """扫描并对账指定账号的当前文件清单。

    Args:
        root: 用户传入的根目录。
        db: 当前根目录对应的数据库。
        accounts: 本次扫描的账号；None 表示全部已发现账号。
        on_progress: 清理、遍历和摘要阶段的进度回调。

    Returns:
        本次扫描、复用和实际摘要读取数量。
    """
    def _emit(phase: str, done: int, total: int, current: str = "") -> None:
        """向调用方发送扫描进度。"""
        if on_progress is not None:
            on_progress(phase, done, total, current)

    selected_accounts = list(accounts) if accounts is not None else discover_accounts(root)

    # ---- 阶段 0：清理临时文件 ----
    _emit("cleaning", 0, 0, "")
    cleanup_tmp_files(root, selected_accounts)
    _emit("cleaning", 1, 1, "")

    # ---- 阶段 1：遍历 + 收集 stat/已存行 ----
    files = _walk_files(root, selected_accounts)
    total_files = len(files)
    infos: list[CurrentFile] = []
    existing = {
        file.rel_path: file
        for file in db.get_current_files(selected_accounts)
    }

    for index, (account, path) in enumerate(files, start=1):
        rel_path = _rel_path(root, path)
        stat_result = path.stat()
        info = CurrentFile(
            rel_path=rel_path,
            account=account,
            size=stat_result.st_size,
            mtime=stat_result.st_mtime_ns,
            content_hash=None,
            volume_id=str(stat_result.st_dev),
            file_id=str(stat_result.st_ino),
            link_count=stat_result.st_nlink,
        )
        infos.append(info)
        if index % 200 == 0 or index == total_files:
            _emit("walking", index, total_files, rel_path)

    _emit("walking", total_files, total_files, "")

    # ---- 阶段 2：按账号和大小预筛，再决定复用或实际读取摘要 ----
    size_counts = Counter((info.account, info.size) for info in infos)
    new_count = 0
    reused_count = 0
    for info in infos:
        old = existing.get(info.rel_path)
        if not old:
            new_count += 1
        is_hash_candidate = size_counts[(info.account, info.size)] >= 2
        if (
            old is not None
            and is_hash_candidate
            and old.size == info.size
            and old.mtime == info.mtime
            and old.content_hash is not None
        ):
            info.content_hash = old.content_hash
            reused_count += 1

    to_hash = [
        info
        for info in infos
        if size_counts[(info.account, info.size)] >= 2
        and info.content_hash is None
    ]
    to_hash_total = len(to_hash)
    _emit("hashing", 0, to_hash_total, "")
    for hashed_count, info in enumerate(to_hash, start=1):
        info.content_hash = compute_hash(root / info.rel_path)
        if hashed_count % 5 == 0 or hashed_count == to_hash_total:
            _emit("hashing", hashed_count, to_hash_total, info.rel_path)

    db.reconcile_current_files(selected_accounts, infos)

    _emit("done", total_files, total_files, "")
    return ScanResult(
        scanned=len(infos),
        new=new_count,
        reused=reused_count,
        hashed=to_hash_total,
    )
