"""纳管、清理与回溯的文件系统执行边界。"""
# 依赖库：psutil
from __future__ import annotations

import errno
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .db import Database
from .models import (
    CurrentFile,
    FileResultRecord,
    FileResultWrite,
    OperationGroupWrite,
    PhysicalIdentity,
)

WECHAT_PROCESS_NAMES: tuple[str, ...] = (
    "WeChat.exe",
    "Weixin.exe",
    "WeChatAppEx.exe",
)
_WECHAT_LOWER = {name.lower() for name in WECHAT_PROCESS_NAMES}


class WechatRunningError(Exception):
    """微信进程正在运行或进程枚举失败。"""


@dataclass(frozen=True)
class LinkGroupPlan:
    """一个重复组内准备执行的来源和链接文件。"""

    content_hash: str
    source: CurrentFile
    targets: list[CurrentFile]


@dataclass(frozen=True)
class RollbackTarget:
    """准备回溯的历史链接文件结果。"""

    content_hash: str
    result: FileResultRecord


@dataclass(frozen=True)
class ExecutionSummary:
    """一次文件操作的执行汇总。"""

    operation_id: int
    succeeded: int
    skipped: int
    failed: int
    disk_full: bool = False


def _enum_processes() -> list[dict[str, str]]:
    """枚举当前进程名称。"""
    import psutil  # type: ignore[import-untyped]

    return [
        {"name": process.info["name"] or ""}
        for process in psutil.process_iter(["name"])
    ]


def is_wechat_running() -> bool:
    """检测微信进程；枚举失败时保守返回运行中。"""
    try:
        processes = _enum_processes()
    except Exception:
        return True
    return any(
        process.get("name", "").lower() in _WECHAT_LOWER
        for process in processes
    )


def _physical_identity(path: Path) -> PhysicalIdentity:
    """读取路径的当前物理文件标识。"""
    stat_result = path.stat()
    return PhysicalIdentity(str(stat_result.st_dev), str(stat_result.st_ino))


def _clear_readonly(path: Path) -> bool:
    """清除路径的只读位，返回清除前是否只读。

    Windows 上对只读文件执行 os.replace/os.unlink 会被拒绝访问，
    临时文件又因 os.link 共享源 inode 属性而同样可能只读。
    """
    mode = path.stat().st_mode
    if not mode & stat.S_IWRITE:
        path.chmod(mode | stat.S_IWRITE)
        return True
    return False


def _restore_readonly(path: Path, was_readonly: bool) -> None:
    """按记录的原状态恢复只读位。"""
    if not was_readonly:
        return
    mode = path.stat().st_mode
    if mode & stat.S_IWRITE:
        path.chmod(mode & ~stat.S_IWRITE)


def _cleanup_temp_file(path: Path) -> None:
    """尽力清理失败操作留下的同目录临时文件。"""
    try:
        _clear_readonly(path)
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _refresh_current_file(db: Database, root: Path, file: CurrentFile) -> CurrentFile:
    """从文件系统刷新一个当前附件并写回当前文件清单。"""
    stat_result = (root / file.rel_path).stat()
    refreshed = CurrentFile(
        rel_path=file.rel_path,
        account=file.account,
        size=stat_result.st_size,
        mtime=stat_result.st_mtime_ns,
        content_hash=file.content_hash,
        volume_id=str(stat_result.st_dev),
        file_id=str(stat_result.st_ino),
        link_count=stat_result.st_nlink,
    )
    db.update_current_file(refreshed)
    return refreshed


def estimate_released_space(plans: list[LinkGroupPlan]) -> int:
    """按硬链接总数保守计算链接替换预计释放的空间。"""
    total = 0
    for plan in plans:
        removed_by_identity: dict[PhysicalIdentity, list[CurrentFile]] = {}
        for target in plan.targets:
            if target.physical_identity == plan.source.physical_identity:
                continue
            removed_by_identity.setdefault(target.physical_identity, []).append(target)
        for targets in removed_by_identity.values():
            if targets[0].link_count <= len(targets):
                total += targets[0].size
    return total


def execute_link_operation(
    db: Database,
    root: Path,
    operation_type: str,
    scope: str,
    account: str,
    plans: list[LinkGroupPlan],
) -> ExecutionSummary:
    """执行纳管或清理，并追加一条完整操作记录。"""
    if is_wechat_running():
        raise WechatRunningError("检测到微信正在运行")
    confirmed_at = datetime.now(timezone.utc).isoformat()
    group_writes: list[OperationGroupWrite] = []
    succeeded = skipped = failed = 0
    for plan in plans:
        source_path = root / plan.source.rel_path
        results: list[FileResultWrite] = []
        try:
            source_identity = _physical_identity(source_path)
        except OSError as error:
            source_missing = isinstance(error, FileNotFoundError)
            results.append(FileResultWrite(
                plan.source.rel_path,
                "source",
                "source",
                "skipped" if source_missing else "failed",
                "路径已丢失" if source_missing else str(error),
                plan.source.physical_identity,
                None,
                plan.source.size,
            ))
            if source_missing:
                skipped += 1
            else:
                failed += 1
            for target in plan.targets:
                results.append(FileResultWrite(
                    target.rel_path, "link", operation_type, "skipped",
                    "来源路径已丢失" if source_missing else "来源路径不可用",
                    target.physical_identity, None, target.size,
                ))
                skipped += 1
            group_writes.append(OperationGroupWrite(
                plan.content_hash, plan.source.rel_path, results,
            ))
            continue
        if source_identity != plan.source.physical_identity:
            results.append(FileResultWrite(
                plan.source.rel_path,
                "source",
                "source",
                "failed",
                "物理文件标识已变化",
                source_identity,
                None,
                plan.source.size,
            ))
            failed += 1
            for target in plan.targets:
                results.append(FileResultWrite(
                    target.rel_path,
                    "link",
                    operation_type,
                    "skipped",
                    "来源文件已变化",
                    target.physical_identity,
                    None,
                    target.size,
                ))
                skipped += 1
            group_writes.append(OperationGroupWrite(
                plan.content_hash, plan.source.rel_path, results,
            ))
            continue

        results.append(FileResultWrite(
            plan.source.rel_path, "source", "source", "success", "",
            source_identity, source_identity, plan.source.size,
        ))
        succeeded += 1
        affected = [plan.source]
        for target in plan.targets:
            target_path = root / target.rel_path
            before_identity = target.physical_identity
            try:
                actual_identity = _physical_identity(target_path)
            except FileNotFoundError:
                results.append(FileResultWrite(
                    target.rel_path, "link", operation_type, "skipped",
                    "路径已丢失", before_identity, None, target.size,
                ))
                skipped += 1
                continue
            except OSError as error:
                results.append(FileResultWrite(
                    target.rel_path, "link", operation_type, "failed",
                    str(error), before_identity, None, target.size,
                ))
                failed += 1
                continue
            if actual_identity != before_identity:
                results.append(FileResultWrite(
                    target.rel_path,
                    "link",
                    operation_type,
                    "failed",
                    "物理文件标识已变化",
                    actual_identity,
                    None,
                    target.size,
                ))
                failed += 1
                continue
            if actual_identity == source_identity:
                action = "confirm_adoption" if operation_type == "adoption" else "cleanup"
                results.append(FileResultWrite(
                    target.rel_path, "link", action, "success", "",
                    actual_identity, actual_identity, target.size,
                ))
                affected.append(target)
                succeeded += 1
                continue
            if source_identity.volume_id != actual_identity.volume_id:
                results.append(FileResultWrite(
                    target.rel_path, "link", operation_type, "skipped", "",
                    actual_identity, None, target.size,
                ))
                skipped += 1
                continue
            temp_path = target_path.with_name(
                f".{target_path.name}.dedup-tmp-{uuid.uuid4().hex}"
            )
            target_was_readonly = False
            try:
                target_was_readonly = _clear_readonly(target_path)
                os.link(source_path, temp_path)
                os.replace(temp_path, target_path)
                _restore_readonly(target_path, target_was_readonly)
                after_identity = _physical_identity(target_path)
                action = "merge_adoption" if operation_type == "adoption" else "cleanup"
                results.append(FileResultWrite(
                    target.rel_path, "link", action, "success", "",
                    actual_identity, after_identity, target.size,
                ))
                affected.append(target)
                succeeded += 1
            except OSError as error:
                _cleanup_temp_file(temp_path)
                _restore_readonly(target_path, target_was_readonly)
                results.append(FileResultWrite(
                    target.rel_path, "link", operation_type, "failed", str(error),
                    actual_identity, None, target.size,
                ))
                failed += 1
        for file in affected:
            if (root / file.rel_path).exists():
                _refresh_current_file(db, root, file)
        group_writes.append(OperationGroupWrite(
            plan.content_hash, plan.source.rel_path, results,
        ))
    operation_id = db.append_operation(
        operation_type,
        scope,
        confirmed_at,
        account,
        group_writes,
    )
    return ExecutionSummary(operation_id, succeeded, skipped, failed)


def execute_rollback(
    db: Database,
    root: Path,
    scope: str,
    account: str,
    targets: list[RollbackTarget],
) -> ExecutionSummary:
    """条件回溯历史链接文件并追加回溯操作记录。"""
    if is_wechat_running():
        raise WechatRunningError("检测到微信正在运行")
    confirmed_at = datetime.now(timezone.utc).isoformat()
    current_by_path = {
        file.rel_path: file for file in db.get_current_files([account])
    }
    latest_result_ids = {
        (result.rel_path, result.after_identity): result.id
        for result in db.get_successful_results([account])
    }
    grouped: dict[str, list[FileResultWrite]] = {}
    succeeded = skipped = failed = 0
    disk_full = False
    for index, target in enumerate(targets):
        result = target.result
        file = current_by_path.get(result.rel_path)
        path = root / result.rel_path
        if file is None:
            grouped.setdefault(target.content_hash, []).append(FileResultWrite(
                result.rel_path, "link", "rollback", "skipped", "路径已丢失",
                result.after_identity, None, result.size,
            ))
            skipped += 1
            continue
        try:
            actual_identity = _physical_identity(path)
        except FileNotFoundError:
            grouped.setdefault(target.content_hash, []).append(FileResultWrite(
                result.rel_path, "link", "rollback", "skipped", "路径已丢失",
                result.after_identity, None, result.size,
            ))
            skipped += 1
            continue
        except OSError as error:
            grouped.setdefault(target.content_hash, []).append(FileResultWrite(
                result.rel_path, "link", "rollback", "failed", str(error),
                file.physical_identity, None, result.size,
            ))
            failed += 1
            continue
        if actual_identity != result.after_identity:
            grouped.setdefault(target.content_hash, []).append(FileResultWrite(
                result.rel_path, "link", "rollback", "skipped", "已经独立",
                actual_identity, actual_identity, result.size,
            ))
            skipped += 1
            continue
        if latest_result_ids.get((result.rel_path, actual_identity)) != result.id:
            grouped.setdefault(target.content_hash, []).append(FileResultWrite(
                result.rel_path,
                "link",
                "rollback",
                "skipped",
                "已有更新的适用操作记录",
                actual_identity,
                actual_identity,
                result.size,
            ))
            skipped += 1
            continue
        temp_path = path.with_name(f".{path.name}.dedup-tmp-{uuid.uuid4().hex}")
        path_was_readonly = False
        try:
            shutil.copy2(path, temp_path)
            path_was_readonly = _clear_readonly(path)
            os.replace(temp_path, path)
            _restore_readonly(path, path_was_readonly)
            refreshed = _refresh_current_file(db, root, file)
            grouped.setdefault(target.content_hash, []).append(FileResultWrite(
                result.rel_path, "link", "rollback", "success", "",
                actual_identity, refreshed.physical_identity, result.size,
            ))
            succeeded += 1
        except OSError as error:
            _cleanup_temp_file(temp_path)
            _restore_readonly(path, path_was_readonly)
            grouped.setdefault(target.content_hash, []).append(FileResultWrite(
                result.rel_path, "link", "rollback", "failed", str(error),
                actual_identity, None, result.size,
            ))
            failed += 1
            if error.errno == errno.ENOSPC:
                disk_full = True
                for remaining in targets[index + 1:]:
                    remaining_file = current_by_path.get(remaining.result.rel_path)
                    remaining_identity = (
                        remaining_file.physical_identity
                        if remaining_file is not None else None
                    )
                    grouped.setdefault(remaining.content_hash, []).append(
                        FileResultWrite(
                            remaining.result.rel_path,
                            "link",
                            "rollback",
                            "skipped",
                            "磁盘空间不足后未处理",
                            remaining_identity,
                            remaining_identity,
                            remaining.result.size,
                        )
                    )
                    skipped += 1
                break
    group_writes = [
        OperationGroupWrite(content_hash, None, results)
        for content_hash, results in grouped.items()
    ]
    operation_id = db.append_operation(
        "rollback",
        scope,
        confirmed_at,
        account,
        group_writes,
    )
    return ExecutionSummary(operation_id, succeeded, skipped, failed, disk_full)
