"""微信附件当前状态、操作历史与条件回溯 TUI。"""
# 依赖库：textual、rich
from __future__ import annotations

import argparse
import shutil
import zlib
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rich.cells import cell_len
from rich.segment import Segment
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import Resize
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import DataTable, Footer

from . import flows
from .db import Database
from .deduper import (
    ExecutionSummary,
    LinkGroupPlan,
    RollbackTarget,
    WechatRunningError,
    estimate_released_space,
    execute_link_operation,
    execute_rollback,
    is_wechat_running,
)
from .dialogs import ConfirmDialog, NoticeDialog, ProgressDialog
from .models import (
    CurrentFile,
    FileResultRecord,
    OperationGroupRecord,
    OperationRecord,
    PhysicalIdentity,
)
from .rules import choose_source, filename, sort_source_candidates
from .scanner import ScanResult, discover_accounts

STATE_PENDING_CLEANUP = "待清理项目"
STATE_PENDING_ADOPTION = "待纳管项目"
STATE_MANAGED = "已纳管项目"
STATE_ROLLED_BACK = "已回溯项目"

VIEW_CURRENT = "current"
VIEW_HISTORY = "history"
LEVEL_ROOT = "root"
LEVEL_ACCOUNT = "account"
LEVEL_GROUP = "group"
LEVEL_HISTORY_OPERATION = "history_operation"
LEVEL_HISTORY_GROUP = "history_group"
LEVEL_HISTORY_FILE = "history_file"
SORT_MODES = ("可省空间", "占用空间", "成员数")
TABLE_CELL_PADDING = 1

OPERATION_LABELS = {
    "adoption": "纳管",
    "cleanup": "清理",
    "rollback": "回溯",
}
SCOPE_LABELS = {
    "physical_group": "物理文件组",
    "file": "单文件",
    "group": "重复组",
    "account": "账号",
}
ROLE_LABELS = {
    "source": "来源文件",
    "link": "链接文件",
}
ACTION_LABELS = {
    "source": "来源文件",
    "adoption": "纳管",
    "confirm_adoption": "确认纳管",
    "merge_adoption": "合并纳管",
    "cleanup": "清理",
    "rollback": "回溯",
}
STATUS_LABELS = {
    "success": "成功",
    "skipped": "跳过",
    "failed": "失败",
}


def format_size(size: int) -> str:
    """把字节数格式化为紧凑的二进制单位。"""
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        value /= 1024
        if value < 1024 or unit == "PB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} PB"


def truncate_path(rel_path: str, max_width: int) -> str:
    """省略目录前缀并优先保留文件名。"""
    from rich.cells import split_graphemes

    if cell_len(rel_path) <= max_width:
        return rel_path
    name = filename(rel_path)
    if cell_len(name) >= max_width - 1:
        suffix = ""
        current_width = 0
        suffix_width = max(max_width - cell_len("…"), 0)
        spans: list[tuple[int, int, int]]
        spans, _total_width = split_graphemes(name)
        for start, end, grapheme_width in reversed(spans):
            if grapheme_width + current_width > suffix_width:
                break
            suffix = name[start:end] + suffix
            current_width += grapheme_width
        return "…" + suffix
    return f"…/{name}"


def truncate_text(text: str, max_width: int) -> str:
    """按终端单元格宽度截断普通文本。

    Args:
        text: 需要显示的文本。
        max_width: 文本可使用的最大终端单元格数。

    Returns:
        未超宽的原文本，或带结尾省略号的截断文本。
    """
    from rich.cells import split_graphemes

    if cell_len(text) <= max_width:
        return text
    prefix = ""
    current_width = 0
    prefix_width = max(max_width - cell_len("…"), 0)
    spans: list[tuple[int, int, int]]
    spans, _total_width = split_graphemes(text)
    for start, end, grapheme_width in spans:
        if grapheme_width + current_width > prefix_width:
            break
        prefix += text[start:end]
        current_width += grapheme_width
    return prefix + "…"


def truncate_path_list(rel_paths: list[str], max_width: int) -> str:
    """压缩路径列表并标出未显示的路径数量。

    Args:
        rel_paths: 需要显示的附件相对路径。
        max_width: 列表可使用的最大终端单元格数。

    Returns:
        完整路径列表、文件名列表或带剩余数量的紧凑摘要。
    """
    full_paths = "；".join(rel_paths)
    if cell_len(full_paths) <= max_width:
        return full_paths
    names = [filename(rel_path) for rel_path in rel_paths]
    all_names = "；".join(names)
    if cell_len(all_names) <= max_width:
        return all_names

    last_fitting = ""
    for index, name in enumerate(names):
        remaining = len(names) - index - 1
        marker = f"；…(+{remaining})" if remaining else ""
        candidate = "；".join([*names[:index], name]) + marker
        if cell_len(candidate) > max_width:
            break
        last_fitting = candidate
    if last_fitting:
        return last_fitting

    marker = f"；…(+{len(names) - 1})" if len(names) > 1 else ""
    name_width = max(max_width - cell_len(marker), 1)
    return truncate_text(truncate_path(rel_paths[0], name_width) + marker, max_width)


def allocate_column_widths(
    columns: list[str],
    rows: list[tuple[list[str], object]],
    width_budget: int,
) -> list[int]:
    """根据标题和内容为所有列分配显式宽度。

    Args:
        columns: 表格列标题。
        rows: 原始表格行和对应载荷。
        width_budget: 扣除滚动条与单元格边距后的总列宽。

    Returns:
        总和不超过预算且由内容需求决定的列宽。
    """
    natural_widths = [
        max(
            cell_len(column),
            max(
                (cell_len(cells[index]) for cells, _payload in rows),
                default=0,
            ),
        )
        for index, column in enumerate(columns)
    ]
    minimum_widths = [cell_len(column) for column in columns]
    usable_budget = max(width_budget, len(columns))

    if sum(minimum_widths) <= usable_budget:
        widths = minimum_widths.copy()
        targets = natural_widths
    else:
        widths = [1] * len(columns)
        targets = minimum_widths

    remaining = usable_budget - sum(widths)
    while remaining > 0:
        pending = [
            index for index, width in enumerate(widths)
            if width < targets[index]
        ]
        if not pending:
            break
        share = max(remaining // len(pending), 1)
        for index in pending:
            added = min(targets[index] - widths[index], share, remaining)
            widths[index] += added
            remaining -= added
            if remaining == 0:
                break

    if remaining > 0:
        flexible_index = max(
            range(len(columns)),
            key=lambda index: (natural_widths[index], -index),
        )
        widths[flexible_index] += remaining
    return widths


def physical_identity(path: Path) -> PhysicalIdentity:
    """读取路径当前的卷标识和文件标识。"""
    stat_result = path.stat()
    return PhysicalIdentity(str(stat_result.st_dev), str(stat_result.st_ino))


@dataclass
class PhysicalGroupInfo:
    """重复组内共享一个物理文件标识的当前成员。"""

    number: int
    identity: PhysicalIdentity
    members: list[CurrentFile]

    @property
    def link_count(self) -> int:
        """返回物理文件数据的硬链接总数。"""
        return max(member.link_count for member in self.members)

    @property
    def label(self) -> str:
        """返回物理文件组显示标签。"""
        return (
            f"物理文件组 {self.number} · 当前 {len(self.members)} 路径"
            f" · 硬链接 {self.link_count}"
        )


@dataclass
class GroupInfo:
    """同一账号内具有相同内容摘要的当前重复组。"""

    account: str
    content_hash: str
    members: list[CurrentFile]
    physical_groups: list[PhysicalGroupInfo]
    states: dict[str, str]
    manual_source_path: str | None = None
    _saveable_cache: int | None = field(default=None, init=False, repr=False)

    @property
    def size(self) -> int:
        """返回单个附件内容大小。"""
        return self.members[0].size

    @property
    def total_size(self) -> int:
        """返回扫描范围内不同物理文件数据的总占用。"""
        return len(self.physical_groups) * self.size

    def state_for(self, member: CurrentFile) -> str:
        """返回一个当前成员的领域状态。"""
        return self.states[member.rel_path]

    def managed_source(self) -> CurrentFile | None:
        """返回适用操作记录覆盖的已纳管来源。"""
        managed = [
            member for member in self.members
            if self.state_for(member) == STATE_MANAGED
        ]
        return choose_source(managed) if managed else None

    def cleanup_source(
        self,
        excluded_paths: set[str] | None = None,
    ) -> CurrentFile | None:
        """按已纳管、手动和自动规则选择清理来源。"""
        excluded = excluded_paths or set()
        managed = self.managed_source()
        if managed is not None and managed.rel_path not in excluded:
            return managed
        candidates = [
            member for member in self.members
            if self.state_for(member) in (STATE_PENDING_CLEANUP, STATE_ROLLED_BACK)
            and member.rel_path not in excluded
        ]
        if self.manual_source_path is not None:
            manual = next(
                (member for member in candidates
                 if member.rel_path == self.manual_source_path),
                None,
            )
            if manual is not None:
                return manual
        return choose_source(candidates) if candidates else None

    def cleanup_plan(self, targets: list[CurrentFile]) -> LinkGroupPlan | None:
        """为给定清理目标构建来源与链接文件计划。"""
        source = self.cleanup_source({target.rel_path for target in targets})
        if source is None:
            source = self.cleanup_source()
            targets = [target for target in targets if target.rel_path != source.rel_path] \
                if source is not None else []
        if source is None or not targets:
            return None
        return LinkGroupPlan(self.content_hash, source, targets)

    @property
    def cleanup_targets(self) -> list[CurrentFile]:
        """返回组级清理可以处理的当前成员。"""
        candidates = [
            member for member in self.members
            if self.state_for(member) in (STATE_PENDING_CLEANUP, STATE_ROLLED_BACK)
        ]
        source = self.cleanup_source()
        return [
            member for member in candidates
            if source is None or member.rel_path != source.rel_path
        ]

    @property
    def saveable(self) -> int:
        """返回完成当前组纳管和清理预计实际释放的空间。"""
        if self._saveable_cache is not None:
            return self._saveable_cache
        source = self.managed_source() or self.cleanup_source()
        if source is None:
            source = choose_source(self.members)
        targets = [
            member for member in self.members
            if member.rel_path != source.rel_path
            and self.state_for(member) != STATE_MANAGED
        ]
        plan = LinkGroupPlan(self.content_hash, source, targets)
        self._saveable_cache = estimate_released_space([plan])
        return self._saveable_cache


@dataclass
class TreeData:
    """账号到当前重复组的内存视图。"""

    by_account: dict[str, list[GroupInfo]] = field(default_factory=dict)

    def accounts(self) -> list[str]:
        """按显示重复组数降序和账号名升序返回账号。"""
        return sorted(
            self.by_account,
            key=lambda account: (-len(self.by_account[account]), account),
        )

    def groups_in_account(self, account: str) -> list[GroupInfo]:
        """返回指定账号的当前重复组。"""
        return self.by_account.get(account, [])


def _applicable_by_path(
    current_files: list[CurrentFile],
    results: list[FileResultRecord],
) -> dict[str, FileResultRecord]:
    """按相对路径和当前物理标识选择最近适用文件结果。"""
    current = {file.rel_path: file for file in current_files}
    applicable: dict[str, FileResultRecord] = {}
    for result in results:
        file = current.get(result.rel_path)
        if file is not None and result.after_identity == file.physical_identity:
            applicable[result.rel_path] = result
    return applicable


def build_tree(
    current_files: list[CurrentFile],
    accounts: list[str],
    successful_results: list[FileResultRecord],
) -> TreeData:
    """构建账号、重复组、物理文件组和当前领域状态。"""
    applicable = _applicable_by_path(current_files, successful_results)
    candidates: dict[tuple[str, str], list[CurrentFile]] = defaultdict(list)
    for file in current_files:
        if file.content_hash is not None:
            candidates[(file.account, file.content_hash)].append(file)
    by_account: dict[str, list[GroupInfo]] = defaultdict(list)
    for account in accounts:
        by_account[account] = []
    for (account, content_hash), members in candidates.items():
        if len(members) < 2:
            continue
        sorted_members = sort_source_candidates(members)
        states: dict[str, str] = {}
        physical_members: dict[PhysicalIdentity, list[CurrentFile]] = defaultdict(list)
        for member in sorted_members:
            result = applicable.get(member.rel_path)
            if result is not None:
                state = STATE_ROLLED_BACK if result.action == "rollback" else STATE_MANAGED
            else:
                state = (
                    STATE_PENDING_ADOPTION
                    if member.link_count > 1 else STATE_PENDING_CLEANUP
                )
            states[member.rel_path] = state
            physical_members[member.physical_identity].append(member)
        physical_groups = [
            PhysicalGroupInfo(number, identity, group_members)
            for number, (identity, group_members) in enumerate(
                physical_members.items(), start=1,
            )
        ]
        if all(state == STATE_MANAGED for state in states.values()):
            continue
        by_account[account].append(GroupInfo(
            account, content_hash, sorted_members, physical_groups, states,
        ))
    return TreeData(dict(by_account))


class StatusBar(Widget):
    """使用 Textual Line API 绘制固定单行状态。"""

    def __init__(self, message: str = "", *, id: str | None = None) -> None:
        """初始化状态文本和组件标识。"""
        super().__init__(id=id)
        self.message = message

    def update(self, message: str) -> None:
        """更新状态文本并请求重绘。"""
        self.message = message
        self.refresh()

    def render_line(self, y: int) -> Strip:
        """直接返回当前状态行的 Segment。"""
        if y != 0:
            return Strip.blank(self.size.width, self.rich_style)
        strip = Strip([Segment(self.message, self.rich_style)])
        return strip.crop_extend(0, self.size.width, self.rich_style)


class ResponsiveDataTable(DataTable[object]):
    """在组件实际宽度变化时通知应用重建响应式列。"""

    def __init__(
        self,
        on_width_changed: Callable[[], None],
        *,
        id: str | None = None,
    ) -> None:
        """初始化宽度变化回调和组件标识。"""
        super().__init__(id=id)
        self._on_width_changed = on_width_changed
        self._layout_width = 0

    def on_resize(self, event: Resize) -> None:
        """实际宽度变化时通知应用，忽略内容引起的虚拟尺寸变化。"""
        if event.size.width == self._layout_width:
            return
        self._layout_width = event.size.width
        self._on_width_changed()


class DedupApp(App[None]):
    """扫描、操作和浏览微信附件状态的 Textual 应用。"""

    CSS = """
    #status { height: 1; background: $boost; padding: 0 1; }
    #duplist { overflow-x: hidden; }
    """
    BINDINGS = [
        Binding("escape", "ascend", "返回"),
        Binding("left", "ascend", "返回"),
        Binding("right", "descend", "进入"),
        Binding("s", "cycle_sort", "排序"),
        Binding("m", "mark_source", "手动来源"),
        Binding("c", "adopt", "纳管"),
        Binding("d", "cleanup", "清理"),
        Binding("h", "history", "操作历史"),
        Binding("u", "rollback", "回溯"),
        Binding("r", "rescan", "重新扫描"),
        Binding("q", "quit", "退出"),
    ]

    def __init__(self, root: Path | None = None) -> None:
        """初始化应用和当前会话状态。"""
        super().__init__()
        self.root = root
        self.db: Database | None = None
        self.discovered: list[str] = []
        self.accounts: list[str] = []
        self.sort_index = 0
        self.manual_sources: dict[tuple[str, str], str] = {}
        self._tree = TreeData()
        self._view = VIEW_CURRENT
        self._level = LEVEL_ROOT
        self._account: str | None = None
        self._group: GroupInfo | None = None
        self._history_operation: OperationRecord | None = None
        self._history_group: OperationGroupRecord | None = None

    def compose(self) -> ComposeResult:
        """构建主界面。"""
        yield StatusBar(id="status")
        yield ResponsiveDataTable(self._on_table_width_changed, id="duplist")
        yield Footer()

    def on_mount(self) -> None:
        """初始化列表并展示已发现账号清单。"""
        table = self.query_one("#duplist", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.cell_padding = TABLE_CELL_PADDING
        if self.root is None:
            self._update_status("请用命令行参数传入 WeChat Files\\ 目录路径")
            return
        self.db = Database(self.root / ".wechat-dedup.db")
        self.db.init_schema()
        self.discovered = discover_accounts(self.root)
        self.accounts = self.db.list_scanned_accounts(self.discovered)
        self.call_after_refresh(self._refresh_current)

    def _on_table_width_changed(self) -> None:
        """DataTable 完成宽度布局后按当前视口重建列表。"""
        if self.db is not None:
            self._render_list()

    def _do_scan(self, account: str) -> None:
        """扫描指定账号：清除该账号手动来源并启动状态对账。"""
        assert self.db is not None and self.root is not None
        for key in [k for k in self.manual_sources if k[0] == account]:
            self.manual_sources.pop(key, None)
        self.push_screen(ProgressDialog())
        self._scan_worker(account)

    @work(thread=True)
    def _scan_worker(self, account: str) -> None:
        """在工作线程中扫描单个账号。"""
        assert self.db is not None and self.root is not None
        try:
            result = flows.run_scan(
                self.db, self.root, [account],
                on_progress=lambda phase, done, total, current: self.call_from_thread(
                    self._update_progress, phase, done, total, current,
                ),
            )
        finally:
            self.call_from_thread(self._pop_progress)
        self.call_from_thread(self._after_scan, account, result)

    def _after_scan(self, account: str, result: ScanResult) -> None:
        """扫描完成后登记已扫描账号、重建视图、进入该账号并显示扫描统计。"""
        if account not in self.accounts:
            self.accounts.append(account)
        self._refresh_current(render=False)
        self._account = account
        self._level = LEVEL_ACCOUNT
        self._group = None
        self._render_list()
        self._refresh_status()
        self._notice(
            f"{account} 扫描完成：共 {result.scanned} 个文件，"
            f"复用摘要 {result.reused} 个，实际计算摘要 {result.hashed} 个"
        )

    def _update_progress(self, phase: str, done: int, total: int, current: str) -> None:
        """更新扫描进度框。"""
        if isinstance(self.screen, ProgressDialog):
            self.screen.update(phase, done, total, current)

    def _pop_progress(self) -> None:
        """关闭扫描进度框。"""
        if isinstance(self.screen, ProgressDialog):
            self.pop_screen()

    def _refresh_current(
        self, reset_navigation: bool = False, render: bool = True,
    ) -> None:
        """从持久状态重建当前视图。"""
        assert self.db is not None
        self._tree = build_tree(
            self.db.get_current_files(self.accounts),
            self.accounts,
            self.db.get_successful_results(self.accounts),
        )
        for account in self._tree.accounts():
            for group in self._tree.groups_in_account(account):
                group.manual_source_path = self.manual_sources.get(
                    (account, group.content_hash)
                )
        if reset_navigation:
            self._view = VIEW_CURRENT
            self._level = LEVEL_ROOT
            self._account = None
            self._group = None
        if render:
            self._render_list()
            self._refresh_status()

    def _columns_for_level(self) -> list[str]:
        """返回当前层级表格列。"""
        if self._view == VIEW_HISTORY:
            if self._level == LEVEL_HISTORY_OPERATION:
                return ["时间", "账号", "操作", "范围", "汇总"]
            if self._level == LEVEL_HISTORY_GROUP:
                return ["重复组", "来源文件", "链接文件"]
            return ["角色", "动作", "结果", "路径", "操作前标识", "操作后标识"]
        if self._level == LEVEL_ROOT:
            return ["账号", "重复组", "占用空间", "可省空间"]
        if self._level == LEVEL_ACCOUNT:
            return ["重复组", "成员", "占用空间", "可省空间"]
        return ["状态", "物理文件组", "路径"]

    def _current_rows(self) -> list[tuple[list[str], object]]:
        """生成当前层级的表格行和载荷。"""
        assert self.db is not None
        if self._view == VIEW_HISTORY:
            if self._level == LEVEL_HISTORY_OPERATION:
                operation_rows: list[tuple[list[str], object]] = []
                for operation in self.db.get_operations(self.accounts):
                    results = [
                        result
                        for group in self.db.get_operation_groups(operation.id)
                        for result in self.db.get_file_results(group.id)
                    ]
                    summary = " / ".join(
                        f"{label}: {sum(result.status == status for result in results)}"
                        for status, label in STATUS_LABELS.items()
                    )
                    operation_rows.append(([
                        operation.confirmed_at,
                        operation.account,
                        OPERATION_LABELS[operation.operation_type],
                        SCOPE_LABELS[operation.scope],
                        summary,
                    ], operation))
                return operation_rows
            if self._level == LEVEL_HISTORY_GROUP:
                assert self._history_operation is not None
                history_groups = self.db.get_operation_groups(self._history_operation.id)
                group_rows: list[tuple[list[str], object]] = []
                for group in history_groups:
                    results = self.db.get_file_results(group.id)
                    link_paths = "；".join(
                        result.rel_path for result in results
                        if result.role == "link"
                    )
                    group_rows.append(([
                        group.content_hash[:12],
                        group.source_rel_path or "-",
                        link_paths or "-",
                    ], group))
                return group_rows
            assert self._history_group is not None
            account = self._history_operation.account
            current_by_path = {
                file.rel_path: file
                for file in self.db.get_current_files(self.accounts)
            }
            _identities, latest_ids = self._rollback_relevance(account)
            return [
                ([ROLE_LABELS[result.role], ACTION_LABELS[result.action],
                  self._history_result_status(result, current_by_path, latest_ids),
                  result.rel_path,
                  self._format_identity(result.before_identity),
                  self._format_identity(result.after_identity)], result)
                for result in self.db.get_file_results(self._history_group.id)
            ]
        if self._level == LEVEL_ROOT:
            rows: list[tuple[list[str], object]] = []
            scanned = set(self._tree.accounts())
            for account in self.discovered:
                if account in scanned:
                    current_groups = self._tree.groups_in_account(account)
                    rows.append(([f"📁 {account}", str(len(current_groups)),
                                  format_size(sum(group.total_size for group in current_groups)),
                                  format_size(sum(
                                      group.saveable for group in current_groups
                                  ))], account))
                else:
                    # 未扫描账号只显示账号名
                    rows.append(([f"📁 {account}", "-", "-", "-"], account))
            return rows
        if self._level == LEVEL_ACCOUNT:
            assert self._account is not None
            return [
                ([f"📄 {filename(group.members[0].rel_path)}",
                  str(len(group.members)), format_size(group.total_size),
                  format_size(group.saveable)], group)
                for group in self._sorted_groups(self._tree.groups_in_account(self._account))
            ]
        assert self._group is not None
        member_rows: list[tuple[list[str], object]] = []
        physical_by_identity = {
            group.identity: group for group in self._group.physical_groups
        }
        for member in self._group.members:
            state = self._group.state_for(member)
            if member.rel_path == self._group.manual_source_path:
                state = f"{state} · 手动来源"
            physical_group = physical_by_identity[member.physical_identity]
            member_rows.append(([
                state,
                physical_group.label,
                member.rel_path,
            ], member))
        return member_rows

    @staticmethod
    def _format_identity(identity: PhysicalIdentity | None) -> str:
        """将物理文件标识格式化为八位 CRC32 短摘要。"""
        if identity is None:
            return "-"
        value = f"{identity.volume_id}\0{identity.file_id}".encode("utf-8")
        return f"{zlib.crc32(value) & 0xffffffff:08X}"

    def _history_result_status(
        self,
        result: FileResultRecord,
        current_by_path: dict[str, CurrentFile],
        latest_ids: dict[str, int],
    ) -> str:
        """根据当前物理标识说明历史文件结果状态。"""
        current = current_by_path.get(result.rel_path)
        current_identity: PhysicalIdentity | None = None
        current_state = "已经丢失"
        if current is not None:
            current_identity = current.physical_identity
            current_state = "当前路径存在"
        if current is not None and self.root is not None:
            try:
                current_identity = physical_identity(self.root / result.rel_path)
            except FileNotFoundError:
                current_identity = None
                current_state = "已经丢失"
            except OSError:
                current_identity = None
                current_state = "当前状态不可用"
        if result.status != "success":
            status = STATUS_LABELS[result.status]
            parts = [status]
            if result.message:
                parts.append(result.message)
            parts.append(current_state)
            return " · ".join(parts)
        if current_identity is None:
            return f"成功 · {current_state}"
        if current_identity != result.after_identity:
            return "成功 · 已经独立"
        if latest_ids.get(result.rel_path) != result.id:
            return "成功 · 已有更新的适用操作记录"
        if result.role == "source":
            return "成功 · 来源文件 · 仍匹配"
        return "成功 · 仍可回溯" if result.action != "rollback" \
            else "成功 · 回溯成功"

    def _sorted_groups(self, groups: list[GroupInfo]) -> list[GroupInfo]:
        """按当前模式降序排序重复组。"""
        keys = (
            lambda group: group.saveable,
            lambda group: group.total_size,
            lambda group: len(group.members),
        )
        return sorted(groups, key=keys[self.sort_index], reverse=True)

    def _column_widths(
        self,
        table: DataTable[object],
        rows: list[tuple[list[str], object]],
    ) -> list[int]:
        """返回当前层级由标题、内容和视口共同决定的显式列宽。"""
        columns = self._columns_for_level()
        padding_width = 2 * TABLE_CELL_PADDING * len(columns)
        available_width = max(table.size.width - 1, 1)
        width_budget = max(available_width - padding_width, len(columns))
        return allocate_column_widths(columns, rows, width_budget)

    def _display_rows(
        self,
        rows: list[tuple[list[str], object]],
        column_widths: list[int],
    ) -> list[tuple[list[str], object]]:
        """按最终列宽生成带明确省略标记的显示行。"""
        display_rows: list[tuple[list[str], object]] = []
        for cells, payload in rows:
            display_cells: list[str] = []
            for index, (cell, width) in enumerate(zip(cells, column_widths)):
                current_path = self._view == VIEW_CURRENT \
                    and self._level == LEVEL_GROUP and index == 2
                history_source = self._view == VIEW_HISTORY \
                    and self._level == LEVEL_HISTORY_GROUP and index == 1
                history_links = self._view == VIEW_HISTORY \
                    and self._level == LEVEL_HISTORY_GROUP and index == 2
                history_path = self._view == VIEW_HISTORY \
                    and self._level == LEVEL_HISTORY_FILE and index == 3
                if current_path or history_source or history_path:
                    display_cells.append(truncate_path(cell, width))
                elif history_links and cell != "-":
                    display_cells.append(truncate_path_list(cell.split("；"), width))
                else:
                    display_cells.append(truncate_text(cell, width))
            display_rows.append((display_cells, payload))
        return display_rows

    def _render_list(self) -> None:
        """重绘当前表格。"""
        table = self.query_one("#duplist", DataTable)
        columns = self._columns_for_level()
        rows = self._current_rows()
        column_widths = self._column_widths(table, rows)
        display_rows = self._display_rows(rows, column_widths)
        with self.batch_update():
            table.clear(columns=True)
            for column, width in zip(columns, column_widths):
                table.add_column(column, width=width)
            table.add_rows(cells for cells, _payload in display_rows)

    def _update_status(self, message: str) -> None:
        """更新 Line API 状态栏。"""
        self.query_one("#status", StatusBar).update(message)

    def _refresh_status(self) -> None:
        """刷新当前范围状态栏。"""
        if self._view == VIEW_HISTORY:
            self._update_status(f"操作历史 · 已扫描账号: {', '.join(self.accounts)}")
            return
        saveable = sum(
            group.saveable for account in self._tree.accounts()
            for group in self._tree.groups_in_account(account)
        )
        location = "全部账号"
        if self._level == LEVEL_ACCOUNT:
            location = self._account or ""
            saveable = sum(
                group.saveable for group in self._tree.groups_in_account(location)
            )
        elif self._level == LEVEL_GROUP and self._group is not None:
            location = f"{self._group.account}/{filename(self._group.members[0].rel_path)}"
            saveable = self._group.saveable
        self._update_status(
            f"位置: {location} · 当前可省空间: {format_size(saveable)}"
        )

    def _current_payload(self) -> object | None:
        """返回光标行载荷。"""
        table = self.query_one("#duplist", DataTable)
        rows = self._current_rows()
        index = table.cursor_row
        return rows[index][1] if index is not None and 0 <= index < len(rows) else None

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        """Enter 选择当前行时进入下一层。"""
        self.action_descend()

    def action_descend(self) -> None:
        """进入当前行的下一层。"""
        payload = self._current_payload()
        if self._view == VIEW_HISTORY:
            if isinstance(payload, OperationRecord):
                self._history_operation = payload
                self._level = LEVEL_HISTORY_GROUP
            elif isinstance(payload, OperationGroupRecord):
                self._history_group = payload
                self._level = LEVEL_HISTORY_FILE
            else:
                return
        elif self._level == LEVEL_ROOT and isinstance(payload, str):
            # 未扫描账号先扫描，完成后由 _after_scan 进入账号层
            if payload not in self.accounts:
                self._do_scan(payload)
                return
            self._account = payload
            self._level = LEVEL_ACCOUNT
        elif self._level == LEVEL_ACCOUNT and isinstance(payload, GroupInfo):
            self._group = payload
            self._level = LEVEL_GROUP
        else:
            return
        self._render_list()
        self._refresh_status()

    def action_ascend(self) -> None:
        """返回上一层或退出历史视图。"""
        if self._view == VIEW_HISTORY:
            if self._level == LEVEL_HISTORY_FILE:
                self._level = LEVEL_HISTORY_GROUP
                self._history_group = None
            elif self._level == LEVEL_HISTORY_GROUP:
                self._level = LEVEL_HISTORY_OPERATION
                self._history_operation = None
            else:
                self.action_history()
                return
        elif self._level == LEVEL_GROUP:
            self._level = LEVEL_ACCOUNT
            self._group = None
        elif self._level == LEVEL_ACCOUNT:
            self._level = LEVEL_ROOT
            self._account = None
        else:
            self.exit()
            return
        self._render_list()
        self._refresh_status()

    def action_cycle_sort(self) -> None:
        """在重复组列表循环切换排序模式。"""
        if self._view == VIEW_CURRENT and self._level == LEVEL_ACCOUNT:
            self.sort_index = (self.sort_index + 1) % len(SORT_MODES)
            self._render_list()

    def action_mark_source(self) -> None:
        """在文件层切换待清理项目的手动来源。"""
        payload = self._current_payload()
        if self._view != VIEW_CURRENT or self._level != LEVEL_GROUP \
                or self._group is None or not isinstance(payload, CurrentFile):
            return
        if self._group.managed_source() is not None \
                or self._group.state_for(payload) != STATE_PENDING_CLEANUP:
            return
        key = (self._group.account, self._group.content_hash)
        self._group._saveable_cache = None
        if self.manual_sources.get(key) == payload.rel_path:
            self.manual_sources.pop(key, None)
            self._group.manual_source_path = None
        else:
            self.manual_sources[key] = payload.rel_path
            self._group.manual_source_path = payload.rel_path
        self._render_list()

    def action_adopt(self) -> None:
        """确认并纳管当前待纳管物理文件组。"""
        payload = self._current_payload()
        if self._view != VIEW_CURRENT or self._level != LEVEL_GROUP \
                or self._group is None or not isinstance(payload, CurrentFile) \
                or self._group.state_for(payload) != STATE_PENDING_ADOPTION:
            return
        physical_group = next(
            group for group in self._group.physical_groups
            if group.identity == payload.physical_identity
        )
        source = self._group.managed_source() or self._group.cleanup_source()
        targets = [
            member for member in physical_group.members
            if self._group.state_for(member) == STATE_PENDING_ADOPTION
        ]
        if source is None:
            source = choose_source(targets)
            targets = [member for member in targets if member.rel_path != source.rel_path]
        plan = LinkGroupPlan(self._group.content_hash, source, targets)
        involved_paths = "\n".join(
            f"- {member.rel_path}" for member in physical_group.members
        )
        message = (
            f"作用域: {physical_group.label}\n来源文件: {source.rel_path}\n"
            f"方式: {'确认纳管' if source.physical_identity == payload.physical_identity else '合并纳管'}\n"
            f"涉及路径: {len(physical_group.members)}\n{involved_paths}\n"
            f"预计释放: {format_size(estimate_released_space([plan]))}"
        )
        self._confirm_link_operation("纳管当前物理文件组？", "adoption", "physical_group", [plan], message)

    def action_cleanup(self) -> None:
        """按账号、重复组或单文件当前范围执行清理。"""
        if self._view != VIEW_CURRENT:
            return
        payload = self._current_payload()
        plans: list[LinkGroupPlan] = []
        scope = ""
        account = ""
        if self._level == LEVEL_GROUP and self._group is not None \
                and isinstance(payload, CurrentFile):
            if self._group.state_for(payload) not in (STATE_PENDING_CLEANUP, STATE_ROLLED_BACK):
                return
            # 手动来源是要留住的文件，不能清理
            if payload.rel_path == self._group.manual_source_path:
                return
            plan = self._group.cleanup_plan([payload])
            plans = [plan] if plan is not None else []
            scope, account = "file", self._group.account
        elif self._level == LEVEL_ACCOUNT and isinstance(payload, GroupInfo):
            plan = payload.cleanup_plan(payload.cleanup_targets)
            plans = [plan] if plan is not None else []
            scope, account = "group", payload.account
        elif self._level == LEVEL_ROOT and isinstance(payload, str):
            account = payload
            plans = [
                plan for group in self._tree.groups_in_account(account)
                if (plan := group.cleanup_plan(group.cleanup_targets)) is not None
            ]
            scope = "account"
        if not plans:
            return
        target_count = sum(len(plan.targets) for plan in plans)
        released = format_size(estimate_released_space(plans))
        if scope == "account":
            # 账号级清理只显示汇总，不逐条列出文件路径
            message = (
                f"作用域: {SCOPE_LABELS[scope]}\n重复组: {len(plans)}\n"
                f"链接文件: {target_count}\n"
                f"预计实际释放: {released}"
            )
        else:
            source_lines = "\n".join(
                f"- {plan.source.rel_path}（{self._source_method(plan)}）"
                for plan in plans
            )
            target_lines = "\n".join(
                f"- {target.rel_path}"
                for plan in plans
                for target in plan.targets
            )
            message = (
                f"作用域: {SCOPE_LABELS[scope]}\n重复组: {len(plans)}\n"
                f"来源文件:\n{source_lines}\n"
                f"链接文件: {target_count}\n"
                f"涉及路径:\n{target_lines}\n"
                f"预计实际释放: {released}"
            )
        self._confirm_link_operation("确认清理？", "cleanup", scope, plans, message, account)

    def _source_method(self, plan: LinkGroupPlan) -> str:
        """返回清理计划的来源选择方式。"""
        for group in self._tree.groups_in_account(plan.source.account):
            if group.content_hash != plan.content_hash:
                continue
            managed = group.managed_source()
            if managed is not None and managed.rel_path == plan.source.rel_path:
                return "适用已纳管来源"
            if group.manual_source_path == plan.source.rel_path:
                return "手动来源"
            return "自动来源"
        return "自动来源"

    def _confirm_link_operation(
        self,
        title: str,
        operation_type: str,
        scope: str,
        plans: list[LinkGroupPlan],
        message: str,
        account: str | None = None,
    ) -> None:
        """执行确认阶段微信守卫并打开危险确认框。"""
        if is_wechat_running():
            self._notice("检测到微信正在运行或进程枚举失败")
            return
        operation_account = account or plans[0].source.account

        def _after_confirm(confirmed: bool | None) -> None:
            """确认后启动链接文件操作。"""
            if confirmed:
                # 保留用户确认时的层位与光标载荷
                anchor = self._capture_anchor()
                self._run_link_operation(
                    operation_type, scope, operation_account, plans, anchor,
                )

        self.push_screen(
            ConfirmDialog(title, message, danger=True),
            _after_confirm,
        )

    @work(thread=True)
    def _run_link_operation(
        self,
        operation_type: str,
        scope: str,
        account: str,
        plans: list[LinkGroupPlan],
        anchor: tuple[object, object],
    ) -> None:
        """在工作线程执行纳管或清理。"""
        assert self.db is not None and self.root is not None
        try:
            summary = execute_link_operation(
                self.db, self.root, operation_type, scope, account, plans,
            )
        except WechatRunningError:
            self.call_from_thread(self._notice, "检测到微信正在运行或进程枚举失败")
            return
        self.call_from_thread(self._after_operation, summary, anchor)

    def _anchor_key_for_payload(self, payload: object) -> str | int | None:
        """返回当前行载荷用于重定位的稳定 key。"""
        if isinstance(payload, str):
            return payload
        if isinstance(payload, (GroupInfo, OperationRecord, OperationGroupRecord, FileResultRecord)):
            key = getattr(payload, "id", None)
            if key is not None:
                return key
            return getattr(payload, "content_hash", None)
        if isinstance(payload, CurrentFile):
            return payload.rel_path
        return None

    def _capture_anchor(self) -> tuple[object, object]:
        """捕获刷新前的层位和当前行载荷，用于刷新后保留导航上下文。"""
        return self._level, self._current_payload()

    def _restore_anchor(self, anchor: tuple[object, object]) -> None:
        """按发起层位保留导航上下文，原目标消失时逐级回退并重定位光标。"""
        level, payload = anchor
        # LEVEL_GROUP：原组在新树里仍含原路径则替换为新引用并保留，否则逐级回退
        if level == LEVEL_GROUP and self._group is not None \
                and isinstance(payload, CurrentFile) and self._account is not None:
            old_hash = self._group.content_hash
            new_group = next(
                (group for group in self._tree.groups_in_account(self._account)
                 if group.content_hash == old_hash),
                None,
            )
            if new_group is not None and any(
                member.rel_path == payload.rel_path for member in new_group.members
            ):
                self._group = new_group
            elif self._tree.groups_in_account(self._account):
                self._level = LEVEL_ACCOUNT
                self._group = None
            else:
                self._level = LEVEL_ROOT
                self._account = None
                self._group = None
        # LEVEL_ACCOUNT：账号下已无组则退到 ROOT
        elif level == LEVEL_ACCOUNT and self._account is not None \
                and not self._tree.groups_in_account(self._account):
            self._level = LEVEL_ROOT
            self._account = None
        # LEVEL_ROOT 与全部历史层级：目标不消失，无需回退
        self._relocate_cursor(payload)

    def _relocate_cursor(self, payload: object) -> None:
        """刷新后把光标重定位到发起操作前的载荷所在行。"""
        key = self._anchor_key_for_payload(payload)
        if key is None:
            return
        rows = self._current_rows()
        for index, (_cells, row_payload) in enumerate(rows):
            if self._anchor_key_for_payload(row_payload) == key:
                table = self.query_one("#duplist", DataTable)
                if 0 <= index < table.row_count:
                    table.move_cursor(row=index)
                return

    def _after_operation(
        self, summary: ExecutionSummary, anchor: tuple[object, object],
    ) -> None:
        """按持久状态刷新，保留发起层位并显示执行汇总。"""
        details = self._operation_result_details(summary.operation_id)
        self._refresh_current(render=False)
        self._restore_anchor(anchor)
        self._render_list()
        self._refresh_status()
        self._notice(
            f"成功 {summary.succeeded}，跳过 {summary.skipped}，失败 {summary.failed}"
            f"{details}"
        )

    def _operation_result_details(self, operation_id: int) -> str:
        """返回操作中需要明确提示的失败或跳过路径。"""
        assert self.db is not None
        detail_lines = [
            f"- {STATUS_LABELS[result.status]}: {result.rel_path}"
            f"（{result.message}）"
            for group in self.db.get_operation_groups(operation_id)
            for result in self.db.get_file_results(group.id)
            if result.status == "failed"
            or (result.status == "skipped" and result.message)
        ]
        detail_text = "\n".join(detail_lines)
        return f"\n涉及路径:\n{detail_text}" if detail_lines else ""

    def action_history(self) -> None:
        """在当前状态和所选账号操作历史之间切换。"""
        if self._view == VIEW_CURRENT:
            self._view = VIEW_HISTORY
            self._level = LEVEL_HISTORY_OPERATION
        else:
            self._view = VIEW_CURRENT
            self._level = LEVEL_ROOT
            self._history_operation = None
            self._history_group = None
        self._render_list()
        self._refresh_status()

    def _rollback_relevance(
        self,
        account: str,
    ) -> tuple[dict[str, PhysicalIdentity], dict[str, int]]:
        """返回当前物理标识及各路径最近适用成功结果。"""
        assert self.db is not None
        identities: dict[str, PhysicalIdentity] = {}
        for file in self.db.get_current_files([account]):
            identity = file.physical_identity
            if self.root is not None:
                try:
                    identity = physical_identity(self.root / file.rel_path)
                except OSError:
                    continue
            identities[file.rel_path] = identity
        latest_result_ids: dict[str, int] = {}
        for result in self.db.get_successful_results([account]):
            if identities.get(result.rel_path) == result.after_identity:
                latest_result_ids[result.rel_path] = result.id
        return identities, latest_result_ids

    @staticmethod
    def _rollback_result_selectable(
        result: FileResultRecord,
        identities: dict[str, PhysicalIdentity],
        latest_result_ids: dict[str, int],
    ) -> bool:
        """判断历史链接结果是否仍是当前关系的最近适用结果。"""
        current_identity = identities.get(result.rel_path)
        return current_identity != result.after_identity \
            or latest_result_ids.get(result.rel_path) == result.id

    def _rollback_targets(self) -> tuple[str, str, list[RollbackTarget]] | None:
        """根据历史层级构建允许的回溯范围。"""
        assert self.db is not None
        payload = self._current_payload()
        if self._level == LEVEL_HISTORY_FILE and isinstance(payload, FileResultRecord):
            if self._history_operation is None or self._history_group is None \
                    or payload.role != "link" or payload.status != "success" \
                    or payload.action == "rollback":
                return None
            identities, latest_ids = self._rollback_relevance(
                self._history_operation.account
            )
            if not self._rollback_result_selectable(
                payload, identities, latest_ids,
            ):
                return None
            return self._history_operation.account, "file", [
                RollbackTarget(self._history_group.content_hash, payload)
            ]
        if self._level == LEVEL_HISTORY_GROUP and isinstance(payload, OperationGroupRecord):
            if self._history_operation is None:
                return None
            identities, latest_ids = self._rollback_relevance(
                self._history_operation.account
            )
            targets = [
                RollbackTarget(payload.content_hash, result)
                for result in self.db.get_file_results(payload.id)
                if result.role == "link" and result.status == "success"
                and result.action != "rollback"
                and self._rollback_result_selectable(
                    result, identities, latest_ids,
                )
            ]
            return self._history_operation.account, "group", targets
        if self._level == LEVEL_HISTORY_OPERATION and isinstance(payload, OperationRecord):
            if payload.operation_type != "cleanup" or payload.scope != "account":
                return None
            identities, latest_ids = self._rollback_relevance(payload.account)
            targets = [
                RollbackTarget(group.content_hash, result)
                for group in self.db.get_operation_groups(payload.id)
                for result in self.db.get_file_results(group.id)
                if result.role == "link" and result.status == "success"
                and result.action != "rollback"
                and self._rollback_result_selectable(
                    result, identities, latest_ids,
                )
            ]
            return payload.account, "account", targets
        return None

    def action_rollback(self) -> None:
        """预览并确认文件、组或账号级条件回溯。"""
        if self._view != VIEW_HISTORY or self.db is None or self.root is None:
            return
        target_scope = self._rollback_targets()
        if target_scope is None:
            return
        account, scope, targets = target_scope
        if not targets:
            return
        current_paths = {
            file.rel_path for file in self.db.get_current_files([account])
        }
        processable: list[RollbackTarget] = []
        missing = independent = 0
        for target in targets:
            if target.result.rel_path not in current_paths:
                missing += 1
                continue
            try:
                identity = physical_identity(self.root / target.result.rel_path)
            except FileNotFoundError:
                missing += 1
                continue
            except OSError:
                independent += 1
                continue
            if identity == target.result.after_identity:
                processable.append(target)
            else:
                independent += 1
        required = sum(target.result.size for target in processable)
        free = shutil.disk_usage(self.root).free
        warning = "\n警告: 预计空间不足，仍可尝试。" if required > free else ""
        message = (
            f"作用域: {SCOPE_LABELS[scope]}\n"
            f"组数: {len({target.content_hash for target in targets})}\n"
            f"可处理: {len(processable)}，已独立: {independent}，已丢失: {missing}\n"
            f"预计新增: {format_size(required)}，当前可用: {format_size(free)}{warning}\n"
            f"涉及路径:\n"
            + "\n".join(f"- {target.result.rel_path}" for target in targets)
        )
        if is_wechat_running():
            self._notice("检测到微信正在运行或进程枚举失败")
            return

        def _after_confirm(confirmed: bool | None) -> None:
            """确认后启动条件回溯。"""
            if confirmed:
                # 回溯在历史视图发起，保留原层位与光标载荷
                anchor = self._capture_anchor()
                self._run_rollback(account, scope, targets, anchor)

        self.push_screen(
            ConfirmDialog("确认回溯？", message, danger=True),
            _after_confirm,
        )

    @work(thread=True)
    def _run_rollback(
        self,
        account: str,
        scope: str,
        targets: list[RollbackTarget],
        anchor: tuple[object, object],
    ) -> None:
        """在工作线程执行条件回溯。"""
        assert self.db is not None and self.root is not None
        try:
            summary = execute_rollback(
                self.db, self.root, scope, account, targets,
            )
        except WechatRunningError:
            self.call_from_thread(self._notice, "检测到微信正在运行或进程枚举失败")
            return
        self.call_from_thread(self._after_rollback, summary, anchor)

    def _after_rollback(
        self, summary: ExecutionSummary, anchor: tuple[object, object],
    ) -> None:
        """刷新当前状态，保留历史视图发起层位并显示回溯结果。"""
        details = self._operation_result_details(summary.operation_id)
        self._refresh_current(render=False)
        self._restore_anchor(anchor)
        self._render_list()
        self._refresh_status()
        suffix = "，磁盘空间不足后已停止" if summary.disk_full else ""
        self._notice(
            f"成功 {summary.succeeded}，跳过 {summary.skipped}，失败 {summary.failed}"
            f"{suffix}{details}"
        )

    def action_rescan(self) -> None:
        """重新扫描当前账号。"""
        if self._view == VIEW_CURRENT and self._account is not None:
            self._do_scan(self._account)

    def _notice(self, message: str) -> None:
        """显示操作提示。"""
        self.push_screen(NoticeDialog("提示", message))


def _existing_dir(value: str) -> Path:
    """argparse 类型校验：路径必须是已存在的目录。"""
    path = Path(value)
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"不是已存在的目录: {value}")
    return path.resolve()


def main() -> None:
    """解析根目录参数并运行应用。"""
    parser = argparse.ArgumentParser(
        prog="wechat-dedup",
        description="微信附件去重工具（NTFS 硬链接）。",
    )
    parser.add_argument(
        "root",
        nargs="?",
        type=_existing_dir,
        help="WeChat Files 目录路径（含若干账号目录）",
    )
    args = parser.parse_args()
    DedupApp(root=args.root).run()


if __name__ == "__main__":
    main()
