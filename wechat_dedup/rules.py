"""留存规则与 rel_path 派生（纯函数，无 I/O）。

ADR-0002: 微信副本后缀语义
ADR-0005: month_dir 正则提取
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol, Sequence, TypeVar

class SourceCandidate(Protocol):
    """自动来源规则所需的附件字段。"""

    rel_path: str
    mtime: int


CandidateT = TypeVar("CandidateT", bound=SourceCandidate)


def filename(rel_path: str) -> str:
    """文件名（最后一段）。"""
    return Path(rel_path).name


def account_id(rel_path: str) -> str:
    """账号目录（第一段）。前缀不固定（ssi_/wxid_/…）。"""
    return Path(rel_path).parts[0]


_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def month_dir(rel_path: str) -> str | None:
    """从目录段提取 YYYY-MM（ADR-0005）。

    只扫除文件名外的目录段、逐段精确匹配、取离文件名最近的命中、无则 None。
    """
    parts = Path(rel_path).parts[:-1]  # 去掉文件名
    for seg in reversed(parts):        # 离文件名最近的优先
        if _MONTH_RE.fullmatch(seg):
            return seg
    return None


# 微信副本后缀：(N) 在 stem 末尾，N 为 1-99 且没有前导零（ADR-0002）。
_WX_COPY_SUFFIX_RE = re.compile(r"\(([1-9]|[1-9]\d)\)$")


def has_wechat_copy_suffix(rel_path: str) -> bool:
    """文件名 stem 末尾是否带微信副本后缀 (N)，N∈{1..99}（ADR-0002）。"""
    stem = Path(rel_path).stem
    return _WX_COPY_SUFFIX_RE.search(stem) is not None


def sort_source_candidates(files: Sequence[CandidateT]) -> list[CandidateT]:
    """按自动来源规则排序候选附件。

    任一候选缺少标准月份时，整组跳过月份比较。其余条件依次为：
    1. 月份信息升序
    2. mtime 升序
    3. has_wechat_copy_suffix（False=0 排前，原始名优先）
    4. rel_path 升序（确定性兜底）

    Args:
        files: 需要排序的来源候选。

    Returns:
        新的稳定排序列表。
    """
    files_list = list(files)
    use_month = all(month_dir(file.rel_path) is not None for file in files_list)

    def _sort_key(file: CandidateT) -> tuple[object, ...]:
        """构建单个候选的自动来源排序键。"""
        month_key = month_dir(file.rel_path) if use_month else ""
        return (
            month_key,
            file.mtime,
            has_wechat_copy_suffix(file.rel_path),
            file.rel_path,
        )

    return sorted(files_list, key=_sort_key)


def choose_source(files: Sequence[CandidateT]) -> CandidateT:
    """返回自动来源规则优先级最高的附件。

    Args:
        files: 非空来源候选。

    Returns:
        自动来源文件。
    """
    return sort_source_candidates(files)[0]
