from __future__ import annotations

import threading
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Callable, Optional

from .bridge.report_bridge import BridgeConfig, BridgeResult, DEFAULT_SOURCE_FILE, DEFAULT_STATE_FILE, ReportBridge


ERROR_PREFIXES = ("error:", "[error]")
MODE_LABELS = {
    "idle": "-",
    "watch": "监听",
    "once": "单次转发",
}
STATE_LABELS = {
    "idle": "空闲",
    "running": "运行中",
    "stopping": "停止中",
    "stopped": "已停止",
    "error": "异常",
}
SOURCE_LABELS = {
    "watch": "监听触发",
    "once": "单次转发",
}


@dataclass(frozen=True)
class LogEntry:
    line: str
    tag: str


@dataclass(frozen=True)
class ResultView:
    text: str
    tag: str


def is_error_message(message: str) -> bool:
    normalized = message.strip().lower()
    return normalized.startswith(ERROR_PREFIXES)


def format_log_entry(message: str, now: Optional[datetime] = None) -> LogEntry:
    current = now or datetime.now()
    tag = "error" if is_error_message(message) else "info"
    level = "ERROR" if tag == "error" else "INFO"
    return LogEntry(line=f"[{current.strftime('%H:%M:%S')}] [{level}] {message}", tag=tag)


def format_state_text(state: str, mode: str) -> str:
    state_label = STATE_LABELS.get(state, state)
    mode_label = MODE_LABELS.get(mode or "idle", mode or "-")
    if state == "idle" and mode in {"", "idle", None}:
        return f"当前状态: {state_label}"
    return f"当前状态: {state_label}（模式: {mode_label}）"


def format_result_view(result: BridgeResult, source: str) -> ResultView:
    source_label = SOURCE_LABELS.get(source, source or "未知")
    text = f"最近一次结果: {result.status} / {result.reason} / 来源: {source_label}"
    tag = "error" if result.status == "error" or is_error_message(result.reason) else "muted"
    return ResultView(text=text, tag=tag)


class BridgeGuiController:
    def __init__(
        self,
        *,
        schedule_ui: Callable[[Callable[[], None]], None],
        bridge_factory: Callable[[BridgeConfig], ReportBridge] = ReportBridge,
    ):
        self._schedule_ui = schedule_ui
        self._bridge_factory = bridge_factory
        self._thread: Optional[threading.Thread] = None
        self._stop_event: Optional[threading.Event] = None
        self._busy = False
        self._run_mode = "idle"
        self._last_run_mode = "idle"
        self.on_log: Callable[[str], None] = lambda message: None
        self.on_state_change: Callable[[str], None] = lambda state: None
        self.on_result: Callable[[BridgeResult], None] = lambda result: None

    @property
    def is_busy(self) -> bool:
        return self._busy

    @property
    def run_mode(self) -> str:
        return self._run_mode

    @property
    def last_run_mode(self) -> str:
        return self._last_run_mode

    def start_watch(self, config: BridgeConfig) -> bool:
        if self._busy:
            return False
        self._stop_event = threading.Event()
        self._run_mode = "watch"
        self._last_run_mode = "watch"
        watch_config = BridgeConfig(
            **{**config.__dict__, "log_callback": self._emit_log, "result_callback": self._emit_result}
        )
        self._launch(watch_config, self._run_watch)
        return True

    def forward_once(self, config: BridgeConfig) -> bool:
        if self._busy:
            return False
        self._stop_event = None
        self._run_mode = "once"
        self._last_run_mode = "once"
        once_config = BridgeConfig(
            **{
                **config.__dict__,
                "watch": False,
                "forward_once": True,
                "log_callback": self._emit_log,
                "result_callback": self._emit_result,
            }
        )
        self._launch(once_config, self._run_once)
        return True

    def stop(self) -> bool:
        if not self._busy or self._stop_event is None:
            return False
        self._stop_event.set()
        self._emit_state("stopping")
        self._emit_log("正在停止监听，请稍候…")
        return True

    def _launch(self, config: BridgeConfig, runner: Callable[[BridgeConfig], None]) -> None:
        self._busy = True
        self._emit_state("running")
        self._thread = threading.Thread(target=runner, args=(config,), daemon=True)
        self._thread.start()

    def _run_watch(self, config: BridgeConfig) -> None:
        try:
            bridge = self._bridge_factory(config)
            exit_code = bridge.run(stop_event=self._stop_event)
            self._emit_log(f"监听结束，exit_code={exit_code}")
            final_state = "stopped" if self._stop_event and self._stop_event.is_set() else "idle"
            self._finish(final_state)
        except Exception as exc:
            self._emit_error_result(exc)
            self._emit_log(f"error: {exc}")
            self._finish("error")

    def _run_once(self, config: BridgeConfig) -> None:
        try:
            bridge = self._bridge_factory(config)
            result = bridge.check_once()
            self._emit_result(result)
            self._emit_log(f"{result.status}: {result.reason}")
            self._finish("idle")
        except Exception as exc:
            self._emit_error_result(exc)
            self._emit_log(f"error: {exc}")
            self._finish("error")

    def _emit_error_result(self, exc: Exception) -> None:
        self._emit_result(BridgeResult(status="error", reason=str(exc)))

    def _finish(self, state: str) -> None:
        self._thread = None
        self._stop_event = None
        self._busy = False
        self._last_run_mode = self._run_mode
        self._run_mode = "idle"
        self._emit_state(state)

    def _emit_log(self, message: str) -> None:
        self._schedule_ui(lambda: self.on_log(message))

    def _emit_state(self, state: str) -> None:
        self._schedule_ui(lambda: self.on_state_change(state))

    def _emit_result(self, result: BridgeResult) -> None:
        self._schedule_ui(lambda: self.on_result(result))


class BridgeGuiApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Bridge GUI")
        self.root.geometry("900x620")
        self.root.minsize(760, 520)

        self.controller = BridgeGuiController(schedule_ui=lambda fn: self.root.after(0, fn))
        self.controller.on_log = self._append_log
        self.controller.on_state_change = self._set_state
        self.controller.on_result = self._set_result

        self._status_var = tk.StringVar(value=format_state_text("idle", "idle"))
        self._result_var = tk.StringVar(value="最近一次结果: -")

        self._source_var = tk.StringVar(value=str(DEFAULT_SOURCE_FILE))
        self._target_var = tk.StringVar()
        self._state_file_var = tk.StringVar(value=str(DEFAULT_STATE_FILE))
        self._poll_seconds_var = tk.StringVar(value="2.0")

        self._watch_var = tk.BooleanVar(value=True)
        self._send_enter_var = tk.BooleanVar(value=False)
        self._allow_plain_text_var = tk.BooleanVar(value=False)
        self._dry_run_var = tk.BooleanVar(value=True)
        self._verbose_var = tk.BooleanVar(value=True)

        self._build_ui()
        self._sync_buttons()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=12)
        container.pack(fill="both", expand=True)

        basic = ttk.LabelFrame(container, text="基础配置")
        basic.pack(fill="x", pady=(0, 8))
        basic.columnconfigure(1, weight=1)
        basic.columnconfigure(4, weight=1)

        ttk.Label(basic, text="source 文件").grid(row=0, column=0, padx=6, pady=6, sticky="w")
        ttk.Entry(basic, textvariable=self._source_var).grid(row=0, column=1, columnspan=3, padx=6, pady=6, sticky="ew")
        ttk.Button(basic, text="选择文件", command=lambda: self._choose_file(self._source_var)).grid(row=0, column=4, padx=6, pady=6, sticky="e")

        ttk.Label(basic, text="target pane").grid(row=1, column=0, padx=6, pady=6, sticky="w")
        ttk.Entry(basic, textvariable=self._target_var).grid(row=1, column=1, padx=6, pady=6, sticky="ew")

        ttk.Label(basic, text="state file").grid(row=1, column=2, padx=6, pady=6, sticky="w")
        ttk.Entry(basic, textvariable=self._state_file_var).grid(row=1, column=3, padx=6, pady=6, sticky="ew")
        ttk.Button(basic, text="选择文件", command=lambda: self._choose_file(self._state_file_var, save=True)).grid(row=1, column=4, padx=6, pady=6, sticky="e")

        ttk.Label(basic, text="poll seconds").grid(row=2, column=0, padx=6, pady=6, sticky="w")
        ttk.Entry(basic, textvariable=self._poll_seconds_var, width=12).grid(row=2, column=1, padx=6, pady=6, sticky="w")

        toggles = ttk.LabelFrame(container, text="行为开关")
        toggles.pack(fill="x", pady=(0, 8))
        for idx, (label, var) in enumerate((
            ("watch", self._watch_var),
            ("send enter", self._send_enter_var),
            ("allow plain text", self._allow_plain_text_var),
            ("dry run", self._dry_run_var),
            ("verbose", self._verbose_var),
        )):
            ttk.Checkbutton(toggles, text=label, variable=var).grid(row=0, column=idx, padx=8, pady=8, sticky="w")

        actions = ttk.LabelFrame(container, text="操作")
        actions.pack(fill="x", pady=(0, 8))
        self._start_button = ttk.Button(actions, text="开始监听", command=self._start_watch)
        self._stop_button = ttk.Button(actions, text="停止监听", command=self._stop_watch)
        self._forward_button = ttk.Button(actions, text="单次转发", command=self._forward_once)
        self._clear_log_button = ttk.Button(actions, text="清空日志", command=self._clear_logs)
        self._start_button.pack(side="left", padx=6, pady=8)
        self._stop_button.pack(side="left", padx=6, pady=8)
        self._forward_button.pack(side="left", padx=6, pady=8)
        self._clear_log_button.pack(side="left", padx=6, pady=8)

        status = ttk.LabelFrame(container, text="状态")
        status.pack(fill="x", pady=(0, 8))
        ttk.Label(status, textvariable=self._status_var).pack(anchor="w", padx=8, pady=(8, 4))
        self._result_label = ttk.Label(status, textvariable=self._result_var, foreground="gray")
        self._result_label.pack(anchor="w", padx=8, pady=(0, 8))

        logs = ttk.LabelFrame(container, text="日志")
        logs.pack(fill="both", expand=True)
        log_frame = ttk.Frame(logs)
        log_frame.pack(fill="both", expand=True, padx=8, pady=8)
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical")
        self._log_text = tk.Text(log_frame, wrap="word", height=18, yscrollcommand=scrollbar.set)
        self._log_text.tag_configure("info", foreground="#333333")
        self._log_text.tag_configure("error", foreground="#b42318")
        self._log_text.tag_configure("muted", foreground="gray")
        scrollbar.config(command=self._log_text.yview)
        self._log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

    def _choose_file(self, variable: tk.StringVar, *, save: bool = False) -> None:
        path = filedialog.asksaveasfilename() if save else filedialog.askopenfilename()
        if path:
            variable.set(path)

    def _build_config(self, *, forward_once: bool = False) -> BridgeConfig:
        poll_seconds = max(float(self._poll_seconds_var.get().strip()), 0.2)
        target_pane = self._target_var.get().strip() or "dry-run:0.0"
        return BridgeConfig(
            source_file=Path(self._source_var.get().strip()),
            target_pane=target_pane,
            state_file=Path(self._state_file_var.get().strip()),
            poll_seconds=poll_seconds,
            watch=self._watch_var.get() and not forward_once,
            forward_once=forward_once,
            send_enter=self._send_enter_var.get(),
            allow_plain_text=self._allow_plain_text_var.get(),
            dry_run=self._dry_run_var.get(),
            verbose=self._verbose_var.get(),
            log_callback=None,
            result_callback=None,
        )

    def _start_watch(self) -> None:
        try:
            config = self._build_config(forward_once=False)
        except Exception as exc:
            self._set_state("error")
            self._set_result(BridgeResult(status="error", reason=str(exc)))
            self._append_log(f"error: {exc}")
            return
        started = self.controller.start_watch(config)
        if not started:
            self._append_log("已有任务在运行")
            return
        self._append_log("开始监听")
        self._sync_buttons()

    def _stop_watch(self) -> None:
        if self.controller.stop():
            self._sync_buttons()

    def _forward_once(self) -> None:
        try:
            config = self._build_config(forward_once=True)
        except Exception as exc:
            self._set_state("error")
            self._set_result(BridgeResult(status="error", reason=str(exc)))
            self._append_log(f"error: {exc}")
            return
        started = self.controller.forward_once(config)
        if not started:
            self._append_log("已有任务在运行")
            return
        self._append_log("开始单次转发")
        self._sync_buttons()

    def _display_mode_for_state(self, state: str) -> str:
        if state in {"running", "stopping"}:
            return self.controller.run_mode
        return self.controller.last_run_mode

    def _set_state(self, state: str) -> None:
        self._status_var.set(format_state_text(state, self._display_mode_for_state(state)))
        self._sync_buttons()

    def _set_result(self, result: BridgeResult) -> None:
        view = format_result_view(result, self.controller.last_run_mode or self.controller.run_mode)
        self._result_var.set(view.text)
        self._result_label.configure(foreground="#b42318" if view.tag == "error" else "gray")

    def _append_log(self, message: str) -> None:
        entry = format_log_entry(message)
        self._log_text.insert("end", f"{entry.line}\n", entry.tag)
        self._log_text.see("end")

    def _clear_logs(self) -> None:
        self._log_text.delete("1.0", "end")
        self._append_log("日志已清空")

    def _sync_buttons(self) -> None:
        busy = self.controller.is_busy
        self._start_button.config(state="disabled" if busy else "normal")
        self._forward_button.config(state="disabled" if busy else "normal")
        self._stop_button.config(state="normal" if busy else "disabled")

    def _on_close(self) -> None:
        if self.controller.is_busy:
            self.controller.stop()
            self.root.after(100, self._poll_close)
            return
        self.root.destroy()

    def _poll_close(self) -> None:
        if self.controller.is_busy:
            self.root.after(100, self._poll_close)
            return
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    BridgeGuiApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
