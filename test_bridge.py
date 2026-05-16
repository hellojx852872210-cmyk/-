from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

from zhuanzhuan_pricing.bridge.parser import build_forward_message, extract_latest_report_block
from zhuanzhuan_pricing.bridge.redaction import redact_sensitive_text
from zhuanzhuan_pricing.bridge.report_bridge import BridgeConfig, ReportBridge


class BridgeTests(unittest.TestCase):
    def test_extract_latest_marked_block(self):
        text = "噪音\n【Claude B 回传】\nold\n\n【Claude B 回传】\nnew"
        parsed = extract_latest_report_block(text)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.normalized_block, "【Claude B 回传】\nnew")

    def test_redaction_masks_sensitive_values(self):
        text = (
            "api_key=secret\n"
            "cookie: abcdefghijklmnopqrstuvwxyz123456\n"
            "https://example.com/hook?token=secret-token&ok=1"
        )
        redacted = redact_sensitive_text(text)
        self.assertIn("api_key=***REDACTED***", redacted)
        self.assertIn("cookie=***REDACTED***", redacted)
        self.assertIn("token=%2A%2A%2AREDACTED%2A%2A%2A", redacted)

    def test_forward_once_skips_duplicate_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "report.txt"
            state = Path(tmp) / "state.json"
            source.write_text("【Claude B 回传】\nhello token=abcdefghijklmnopqrstuvwxyz123456", encoding="utf-8")
            config = BridgeConfig(
                source_file=source,
                target_pane="dummy:0.1",
                state_file=state,
                dry_run=True,
                forward_once=True,
            )
            bridge = ReportBridge(config)
            first = bridge.check_once()
            second = bridge.check_once()
            self.assertEqual(first.status, "forwarded")
            self.assertEqual(second.status, "skipped")
            self.assertIn(second.reason, {"source 未变化", "duplicate content"})

    def test_build_message_wraps_metadata(self):
        parsed = extract_latest_report_block("【Claude B 回传】\nhello")
        wrapped = build_forward_message(parsed, "/tmp/report.txt", datetime(2026, 4, 8, 15, 0, 0))
        self.assertIn("来源文件: /tmp/report.txt", wrapped.wrapped_message)
        self.assertIn("以下是 Claude B 最新回传", wrapped.wrapped_message)

    def test_run_stops_when_stop_event_is_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "report.txt"
            state = Path(tmp) / "state.json"
            source.write_text("【Claude B 回传】\nhello", encoding="utf-8")
            logs: list[str] = []
            config = BridgeConfig(
                source_file=source,
                target_pane="dummy:0.1",
                state_file=state,
                dry_run=True,
                watch=True,
                poll_seconds=0.01,
                log_callback=logs.append,
            )
            bridge = ReportBridge(config)
            stop_event = threading.Event()
            result_holder: dict[str, int] = {}

            thread = threading.Thread(target=lambda: result_holder.setdefault("code", bridge.run(stop_event=stop_event)))
            thread.start()
            time.sleep(0.05)
            stop_event.set()
            thread.join(timeout=1)

            self.assertFalse(thread.is_alive())
            self.assertEqual(result_holder.get("code"), 0)
            self.assertTrue(any("收到停止请求" in message for message in logs))


if __name__ == "__main__":
    unittest.main()
