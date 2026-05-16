from __future__ import annotations

import argparse
from pathlib import Path

from .bridge.report_bridge import BridgeConfig, DEFAULT_SOURCE_FILE, DEFAULT_STATE_FILE, ReportBridge


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Forward Claude B reports into a tmux pane.")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE_FILE), help="Source report file path")
    parser.add_argument("--target-pane", required=True, help="tmux pane target, e.g. session:0.1")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), help="Bridge state file path")
    parser.add_argument("--poll-seconds", type=float, default=2.0, help="Polling interval in seconds")
    parser.add_argument("--watch", action="store_true", help="Keep watching the source file")
    parser.add_argument("--forward-once", action="store_true", help="Forward once then exit")
    parser.add_argument("--send-enter", action="store_true", help="Send Enter after the message")
    parser.add_argument("--allow-plain-text", action="store_true", help="Allow whole file forwarding without marker")
    parser.add_argument("--dry-run", action="store_true", help="Print the payload without sending to tmux")
    parser.add_argument("--verbose", action="store_true", help="Print detection and skip reasons")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.watch and args.forward_once:
        raise SystemExit("--watch 和 --forward-once 不能同时使用")
    config = BridgeConfig(
        source_file=Path(args.source),
        target_pane=args.target_pane,
        state_file=Path(args.state_file),
        poll_seconds=max(args.poll_seconds, 0.2),
        watch=args.watch,
        forward_once=args.forward_once,
        send_enter=args.send_enter,
        allow_plain_text=args.allow_plain_text,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )
    return ReportBridge(config).run()


if __name__ == "__main__":
    raise SystemExit(main())
