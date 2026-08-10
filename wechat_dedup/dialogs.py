"""可复用的 dialog / whiptail 风格弹窗。

每个 ModalScreen 收集或确认输入，再把结果返回调用方。
焦点切换统一使用框架默认的 Tab/Shift+Tab 行为。
"""
# 依赖库：textual
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, ProgressBar, Static


# 进度阶段 → 中文标签
_PHASE_LABELS = {
    "cleaning": "清理临时文件",
    "walking": "遍历文件",
    "hashing": "计算内容摘要",
    "done": "完成",
}


class ConfirmDialog(ModalScreen[bool]):
    """左右按钮确认框。

    Tab/Shift+Tab 在 取消/确认 间切换，Enter 执行，Esc 取消。
    返回 True（确认）或 False（取消/Esc）。danger=True 时确认按钮用 error 变体
    且默认聚焦取消，以安全选项作为初始焦点。
    """
    DEFAULT_CSS = """
    ConfirmDialog { align: center middle; }
    #dialog {
        width: 72; max-width: 90%; height: auto;
        border: thick $background 80%; background: $surface; padding: 1 2;
    }
    #title { text-style: bold; margin-bottom: 1; }
    #body { margin-bottom: 1; }
    #buttons { height: auto; align-horizontal: right; }
    Button { margin-left: 1; }
    """

    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(self, title: str, message: str, *, danger: bool = False) -> None:
        """保存标题、正文和危险操作焦点策略。"""
        super().__init__()
        self.dialog_title = title
        self.message = message
        self.danger = danger

    def compose(self) -> ComposeResult:
        """生成正文及取消、确认按钮。"""
        with Vertical(id="dialog"):
            yield Label(self.dialog_title, id="title")
            yield Static(self.message, id="body")
            with Horizontal(id="buttons"):
                yield Button("取消", id="cancel", variant="default")
                yield Button("确认", id="ok",
                             variant="error" if self.danger else "primary")

    def on_mount(self) -> None:
        """按操作风险设置初始焦点。"""
        # danger 时默认聚焦取消（安全）；否则聚焦确认
        self.query_one("#cancel" if self.danger else "#ok", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """将点击的确认状态返回调用方。"""
        self.dismiss(event.button.id == "ok")

    def action_cancel(self) -> None:
        """取消当前确认流程。"""
        self.dismiss(False)


class NoticeDialog(ModalScreen[None]):
    """单按钮提示框。Tab 聚焦按钮，Enter/Esc 关闭。"""
    DEFAULT_CSS = """
    NoticeDialog { align: center middle; }
    #dialog {
        width: 72; max-width: 90%; height: auto;
        border: thick $background 80%; background: $surface; padding: 1 2;
    }
    #title { text-style: bold; margin-bottom: 1; }
    #body { margin-bottom: 1; }
    #buttons { height: auto; align-horizontal: right; }
    """

    BINDINGS = [("escape", "close", "关闭")]

    def __init__(self, title: str, message: str) -> None:
        """保存提示框标题和正文。"""
        super().__init__()
        self.dialog_title = title
        self.message = message

    def compose(self) -> ComposeResult:
        """生成提示正文和关闭按钮。"""
        with Vertical(id="dialog"):
            yield Label(self.dialog_title, id="title")
            yield Static(self.message, id="body")
            with Horizontal(id="buttons"):
                yield Button("确定", id="ok", variant="primary")

    def on_mount(self) -> None:
        """将初始焦点放到关闭按钮。"""
        self.query_one("#ok", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """点击按钮时关闭提示框。"""
        self.dismiss(None)

    def action_close(self) -> None:
        """关闭当前提示框。"""
        self.dismiss(None)


class ProgressDialog(ModalScreen[None]):
    """进度弹窗：标题 + 状态行 + 原生 ProgressBar。

    扫描期间阻塞交互。由调用方 push 后通过 update() 跨线程驱动，完成后 pop。
    原生 ProgressBar widget 自动渲染横条并适配可用宽度。
    """
    DEFAULT_CSS = """
    ProgressDialog { align: center middle; }
    #dialog {
        width: 72; max-width: 90%; height: auto;
        border: thick $background 80%; background: $surface; padding: 1 2;
    }
    #title { text-style: bold; margin-bottom: 1; }
    #line1 { height: 1; margin-bottom: 1; }
    """

    def __init__(self) -> None:
        """初始化扫描进度弹窗。"""
        super().__init__()
        self._total = 100

    def compose(self) -> ComposeResult:
        """生成阶段文本和进度条。"""
        with Vertical(id="dialog"):
            yield Label("扫描中", id="title")
            yield Static(Text("准备中…", no_wrap=True, overflow="ellipsis"), id="line1")
            yield ProgressBar(total=100, show_eta=False, id="bar")

    def update(self, phase: str, done: int, total: int, current: str) -> None:
        """更新状态行 + 进度条。total=0 时进度条置 0（未知总量）。"""
        label = _PHASE_LABELS.get(phase, phase)
        ratio = done / total if total else 0.0
        pct = round(ratio * 100)
        cur = f"  {current}" if current else ""
        message = f"{label} {done}/{total or '?'}  {pct}%{cur}"
        self.query_one("#line1", Static).update(
            Text(message, no_wrap=True, overflow="ellipsis"),
            layout=False,
        )
        bar = self.query_one("#bar", ProgressBar)
        bar.progress = pct
