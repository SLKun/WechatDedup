"""当前文件清单、操作历史与物理文件标识的数据模型。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class PhysicalIdentity:
    """由卷标识和 NTFS 文件标识组成的物理文件标识。"""

    volume_id: str
    file_id: str


@dataclass
class CurrentFile:
    """当前文件清单中的一个附件。"""

    rel_path: str
    account: str
    size: int
    mtime: int
    content_hash: str | None
    volume_id: str
    file_id: str
    link_count: int

    @property
    def physical_identity(self) -> PhysicalIdentity:
        """返回当前附件的物理文件标识。"""
        return PhysicalIdentity(self.volume_id, self.file_id)


@dataclass(frozen=True)
class FileResultWrite:
    """一次操作中准备追加的文件结果。"""

    rel_path: str
    role: str
    action: str
    status: str
    message: str
    before_identity: PhysicalIdentity | None
    after_identity: PhysicalIdentity | None
    size: int


@dataclass(frozen=True)
class OperationGroupWrite:
    """一次操作中准备追加的重复组结果。"""

    content_hash: str
    source_rel_path: str | None
    file_results: list[FileResultWrite]


@dataclass(frozen=True)
class OperationRecord:
    """追加式操作历史中的一次操作记录。"""

    id: int
    operation_type: str
    scope: str
    confirmed_at: str
    account: str


@dataclass(frozen=True)
class OperationGroupRecord:
    """操作记录中的一个重复组。"""

    id: int
    operation_id: int
    content_hash: str
    source_rel_path: str | None


@dataclass(frozen=True)
class FileResultRecord:
    """操作历史中的一个文件结果。"""

    id: int
    operation_group_id: int
    rel_path: str
    role: str
    action: str
    status: str
    message: str
    before_identity: PhysicalIdentity | None
    after_identity: PhysicalIdentity | None
    size: int
