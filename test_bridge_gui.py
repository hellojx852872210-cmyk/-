from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from zhuanzhuan_pricing.bridge.report_bridge import BridgeConfig, BridgeResult
from zhuanzhuan_pricing.bridge_gui import (
    BridgeGuiController,
    format_log_entry,
    format_result_view,
    format_state_text,
)


class _FakeBridge:
    def __init__(self, config: BridgeConfig):
        self.config = config

    def run(self, stop_event=None):
        if self.config.log_callback:
            self.config.log_callback("watch-started")
        if self.config.result_callback:
            self.config.result_callback(BridgeResult(status="skipped", reason="source 未变化"))
        while stop_event is not None and not stop_event.wait(0.01):
            pass
        if self.config.log_callback:
            self.config.log_callback("watch-stopped")
        return 0

    def check_once(self):
        if self.config.log_callback:
            self.config.log_callback("once-ran")
        return BridgeResult(status="forwarded", reason="ok")


class _ErrorBridge:
    def __init__(self, config: BridgeConfig):
        self.config = config

    def check_once(self):
        raise RuntimeError("boom")


class BridgeGuiHelpersTests(unittest.TestCase):
    def test_format_state_text_includes_mode(self):
        self.assertEqual(format_state_text("running", "watch"), "当前状态: 运行中（模式: 监听）")
        self.assertEqual(format_state_text("stopped", "watch"), "当前状态: 已停止（模式: 监听）")
        self.assertEqual(format_state_text("idle", "idle"), "当前状态: 空闲")

    def test_format_log_entry_marks_error_and_timestamp(self):
        entry = format_log_entry("error: boom", now=datetime(2026, 4, 9, 12, 34, 56))
        self.assertEqual(entry.tag, "error")
        self.assertEqual(entry.line, "[12:34:56] [ERROR] error: boom")

    def test_format_result_view_shows_source_and_error(self):
        view = format_result_view(BridgeResult(status="error", reason="boom"), "once")
        self.assertEqual(view.tag, "error")
        self.assertIn("来源: 单次转发", view.text)
        self.assertIn("error / boom", view.text)


class BridgeGuiControllerTests(unittest.TestCase):
    def test_start_watch_and_stop(self):
        events: list[tuple[str, str]] = []
        results: list[BridgeResult] = []
        controller = BridgeGuiController(schedule_ui=lambda fn: fn(), bridge_factory=_FakeBridge)
        controller.on_log = lambda message: events.append(("log", message))
        controller.on_state_change = lambda state: events.append(("state", state))
        controller.on_result = results.append

        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                source_file=Path(tmp) / "report.txt",
                target_pane="dummy:0.1",
                state_file=Path(tmp) / "state.json",
                watch=True,
            )
            self.assertTrue(controller.start_watch(config))
            time.sleep(0.03)
            self.assertTrue(controller.is_busy)
            self.assertFalse(controller.start_watch(config))
            self.assertTrue(controller.stop())

            deadline = time.time() + 1
            while controller.is_busy and time.time() < deadline:
                time.sleep(0.01)

        self.assertFalse(controller.is_busy)
        self.assertEqual(controller.last_run_mode, "watch")
        self.assertIn(("state", "running"), events)
        self.assertIn(("state", "stopping"), events)
        self.assertIn(("state", "stopped"), events)
        self.assertIn(("log", "watch-started"), events)
        self.assertIn(("log", "watch-stopped"), events)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].reason, "source 未变化")

    def test_forward_once_emits_result(self):
        results: list[BridgeResult] = []
        events: list[tuple[str, str]] = []
        controller = BridgeGuiController(schedule_ui=lambda fn: fn(), bridge_factory=_FakeBridge)
        controller.on_log = lambda message: events.append(("log", message))
        controller.on_state_change = lambda state: events.append(("state", state))
        controller.on_result = results.append

        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                source_file=Path(tmp) / "report.txt",
                target_pane="dummy:0.1",
                state_file=Path(tmp) / "state.json",
            )
            self.assertTrue(controller.forward_once(config))

            deadline = time.time() + 1
            while controller.is_busy and time.time() < deadline:
                time.sleep(0.01)

        self.assertFalse(controller.is_busy)
        self.assertEqual(controller.last_run_mode, "once")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "forwarded")
        self.assertIn(("log", "once-ran"), events)
        self.assertIn(("state", "idle"), events)

    def test_forward_once_error_emits_error_result_and_state(self):
        results: list[BridgeResult] = []
        events: list[tuple[str, str]] = []
        controller = BridgeGuiController(schedule_ui=lambda fn: fn(), bridge_factory=_ErrorBridge)
        controller.on_log = lambda message: events.append(("log", message))
        controller.on_state_change = lambda state: events.append(("state", state))
        controller.on_result = results.append

        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                source_file=Path(tmp) / "report.txt",
                target_pane="dummy:0.1",
                state_file=Path(tmp) / "state.json",
            )
            self.assertTrue(controller.forward_once(config))

            deadline = time.time() + 1
            while controller.is_busy and time.time() < deadline:
                time.sleep(0.01)

        self.assertFalse(controller.is_busy)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "error")
        self.assertEqual(results[0].reason, "boom")
        self.assertIn(("log", "error: boom"), events)
        self.assertIn(("state", "error"), events)


if __name__ == "__main__":
    unittest.main()
