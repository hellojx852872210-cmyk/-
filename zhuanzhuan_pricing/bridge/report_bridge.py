from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import atomic_write_json, ensure_parent_dir, paths
from .parser import build_forward_message, extract_latest_report_block, stable_content_hash
from .redaction import redact_sensitive_text
from .tmux_sender import TmuxSender

DEFAULT_SOURCE_FILE = paths.runtime_dir / "claude_b_report.txt"
DEFAULT_STATE_FILE = paths.runtime_dir / "claude_bridge_state.json"
LogCallback = Callable[[str], None]
ResultCallback = Callable[["BridgeResult"], None]


@dataclass
class BridgeConfig:
    source_file: Path
    target_pane: str
    state_file: Path = DEFAULT_STATE_FILE
    poll_seconds: float = 2.0
    watch: bool = False
    forward_once: bool = False
    send_enter: bool = False
    allow_plain_text: bool = False
    dry_run: bool = False
    verbose: bool = False
    log_callback: Optional[LogCallback] = None
    result_callback: Optional[ResultCallback] = None


@dataclass
class BridgeResult:
    status: str
    reason: str
    forwarded_hash: str = ""
    source_hash: str = ""
    source_mtime: float = 0.0
    source_size: int = 0


class ReportBridge:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.state = self._load_state()

    def _log(self, message: str) -> None:
        callback = self.config.log_callback
        if callable(callback):
            callback(message)
        if self.config.verbose:
            print(f"[bridge] {message}")

    def _emit_result(self, result: BridgeResult) -> None:
        callback = self.config.result_callback
        if callable(callback):
            callback(result)

    def _load_state(self) -> dict[str, Any]:
        state_file = self.config.state_file
        if not state_file.exists():
            return {}
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self, result: BridgeResult, forwarded_at: Optional[datetime] = None) -> None:
        payload = {
            "last_seen_hash": result.source_hash,
            "last_forwarded_hash": result.forwarded_hash or self.state.get("last_forwarded_hash", ""),
            "last_forwarded_at": forwarded_at.isoformat(timespec="seconds") if forwarded_at else self.state.get("last_forwarded_at"),
            "last_source_mtime": result.source_mtime,
            "last_source_size": result.source_size,
            "source_file": str(self.config.source_file),
            "target_pane": self.config.target_pane,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        ensure_parent_dir(self.config.state_file)
        atomic_write_json(self.config.state_file, payload)
        self.state = payload

    def _read_source_snapshot(self) -> tuple[str, float, int]:
        source = self.config.source_file
        if not source.exists():
            raise FileNotFoundError(f"source 文件不存在: {source}")
        stat = source.stat()
        text = source.read_text(encoding="utf-8")
        return text, stat.st_mtime, stat.st_size

    def check_once(self) -> BridgeResult:
        text, mtime, size = self._read_source_snapshot()
        source_hash = stable_content_hash(text)

        if not text.strip():
            result = BridgeResult("skipped", "source 文件为空", source_hash=source_hash, source_mtime=mtime, source_size=size)
            self._save_state(result)
            return result

        if (
            source_hash == self.state.get("last_seen_hash")
            and mtime == self.state.get("last_source_mtime")
            and size == self.state.get("last_source_size")
        ):
            return BridgeResult("skipped", "source 未变化", source_hash=source_hash, source_mtime=mtime, source_size=size)

        parsed = extract_latest_report_block(text, allow_plain_text=self.config.allow_plain_text)
        if parsed is None:
            result = BridgeResult("skipped", "未找到可转发的完整回传块", source_hash=source_hash, source_mtime=mtime, source_size=size)
            self._save_state(result)
            return result

        parsed.normalized_block = redact_sensitive_text(parsed.normalized_block)
        forwarded_hash = stable_content_hash(parsed.normalized_block)
        if forwarded_hash == self.state.get("last_forwarded_hash"):
            result = BridgeResult("skipped", "duplicate content", forwarded_hash=forwarded_hash, source_hash=source_hash, source_mtime=mtime, source_size=size)
            self._save_state(result)
            return result

        forwarded_at = datetime.now()
        parsed = build_forward_message(parsed, self.config.source_file, forwarded_at)

        if self.config.dry_run:
            print(parsed.wrapped_message)
            self._log("dry-run: payload ready")
        else:
            sender = TmuxSender(self.config.target_pane)
            sender.send_text(parsed.wrapped_message, send_enter=self.config.send_enter)

        result = BridgeResult("forwarded", "ok", forwarded_hash=forwarded_hash, source_hash=source_hash, source_mtime=mtime, source_size=size)
        self._save_state(result, forwarded_at=forwarded_at)
        return result

    def run(self, stop_event: Optional[threading.Event] = None) -> int:
        while True:
            if stop_event is not None and stop_event.is_set():
                self._log("收到停止请求，退出")
                return 0
            try:
                result = self.check_once()
                self._emit_result(result)
                self._log(f"{result.status}: {result.reason}")
                if self.config.forward_once:
                    return 0 if result.status in {"forwarded", "skipped"} else 1
            except KeyboardInterrupt:
                self._log("收到中断信号，退出")
                return 130
            except Exception as exc:
                self._log(f"error: {exc}")
                if self.config.forward_once:
                    raise
            if not self.config.watch:
                return 0
            if stop_event is not None and stop_event.wait(self.config.poll_seconds):
                self._log("收到停止请求，退出")
                return 0
            time.sleep(self.config.poll_seconds) if stop_event is None else None
