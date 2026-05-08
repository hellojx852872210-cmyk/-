# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import datetime
import getpass
import os
import time
from typing import Callable

from ..core.models import Account
from ..core.rule_engine import RuleEngine
from ..core.batch_store import BatchItemStore
from ..services.data_store import AccountStore, SoldCache, CostPriceMap
from ..services.erp_service import ErpConfig
from ..services.zhuanzhuan_api import ImeiService
from .tasks import (
    MANUAL_REVIEW_ACTION_REJECT_AND_IGNORE,
    MANUAL_REVIEW_STATE_PENDING,
    apply_manual_review_batch_decision,
    task_auto_list,
    task_auto_reprice,
    task_erp_sync,
    task_sales_report,
    task_stale_drop,
)


def _now_text() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str) -> None:
    print(f"[{_now_text()}] {msg}")


class AgentRuntime:
    def __init__(self):
        self.account_store = AccountStore()
        self.sold_cache = SoldCache()
        self.cost_map = CostPriceMap()
        self.rule_engine = RuleEngine()
        self.erp_config = ErpConfig()
        self.imported_store = BatchItemStore()


TaskFunc = Callable[[AgentRuntime], dict | str]


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _fmt_money(value) -> str:
    try:
        return f"{float(value):.0f}"
    except Exception:
        return "-"


def _format_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        widths = [len(h) for h in headers]
    else:
        widths = [len(h) for h in headers]
        for row in rows:
            for idx, cell in enumerate(row):
                widths[idx] = max(widths[idx], len(str(cell)))

    def _line(parts: list[str]) -> str:
        return "| " + " | ".join(str(part).ljust(widths[idx]) for idx, part in enumerate(parts)) + " |"

    sep = "+-" + "-+-".join("-" * width for width in widths) + "-+"
    lines = [sep, _line(headers), sep]
    for row in rows:
        lines.append(_line(row))
    lines.append(sep)
    return "\n".join(lines)


def _build_task_summary_row(task_name: str, result: dict | str | None, error: str = "") -> list[str]:
    if error:
        return [task_name, "-", "-", "-", "1", "-", "-", error[:48]]
    data = result if isinstance(result, dict) else {}
    total = _to_int(data.get("total"), 0)
    ok = _to_int(data.get("ok"), 0)
    skip = _to_int(data.get("skip"), 0)
    fail = _to_int(data.get("fail"), 0)
    manual_review = _to_int(data.get("manual_review"), 0)
    persisted = _to_int(data.get("persisted_count"), 0)
    note = ""
    if not isinstance(result, dict):
        note = str(result or "")[:48]
    elif task_name == "sales_report":
        note = str(data.get("report") or "")[:48]
    return [task_name, str(total), str(ok), str(skip), str(fail), str(manual_review), str(persisted), note]


def _collect_persisted_rows(task_name: str, result: dict | str | None) -> list[list[str]]:
    if not isinstance(result, dict):
        return []
    rows: list[list[str]] = []
    for sample in list(result.get("persisted_samples") or []):
        if not isinstance(sample, dict):
            continue
        rows.append([
            str(sample.get("task") or task_name),
            str(sample.get("account") or "-"),
            str(sample.get("item") or "-"),
            _fmt_money(sample.get("old_price")),
            _fmt_money(sample.get("new_price")),
            _fmt_money(sample.get("diff")),
            str(sample.get("trigger") or "-"),
        ])
    return rows


def _run_erp_sync(runtime: AgentRuntime):
    return task_erp_sync(
        runtime.erp_config,
        runtime.cost_map,
        runtime.account_store,
        imported_store=runtime.imported_store,
        on_progress=_log,
        sold_cache=runtime.sold_cache,
        sold_sync_days=30,
    )


def _run_auto_reprice(runtime: AgentRuntime):
    return task_auto_reprice(
        runtime.account_store,
        runtime.sold_cache,
        runtime.cost_map,
        runtime.rule_engine,
        runtime.imported_store,
        on_progress=_log,
    )


def _run_stale_drop(runtime: AgentRuntime):
    return task_stale_drop(
        runtime.account_store,
        runtime.sold_cache,
        runtime.rule_engine,
        runtime.imported_store,
        on_progress=_log,
    )


def _run_auto_list(runtime: AgentRuntime):
    return task_auto_list(
        runtime.account_store,
        runtime.erp_config,
        runtime.sold_cache,
        runtime.rule_engine,
        runtime.imported_store,
        on_progress=_log,
    )


def _run_sales_report(runtime: AgentRuntime):
    return {
        "report": task_sales_report(
            runtime.account_store,
            notifier=None,
            on_progress=_log,
            imported_store=runtime.imported_store,
            wx_app_client=None,
        )
    }


TASKS: dict[str, TaskFunc] = {
    "erp_sync": _run_erp_sync,
    "auto_reprice": _run_auto_reprice,
    "stale_drop": _run_stale_drop,
    "auto_list": _run_auto_list,
    "sales_report": _run_sales_report,
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Headless agent runner for zhuanzhuan automation")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run scheduled automation loop")
    run_parser.add_argument(
        "--tasks",
        default="erp_sync,auto_reprice",
        help="Comma-separated task list. Available: erp_sync,auto_reprice,stale_drop,auto_list,sales_report",
    )
    run_parser.add_argument("--interval-seconds", type=int, default=300, help="Interval between cycles")
    run_parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    run_parser.add_argument("--max-cycles", type=int, default=0, help="Stop after N cycles (0 means unlimited)")

    config_parser = subparsers.add_parser("config", help="Manage multi-store account config")
    config_sub = config_parser.add_subparsers(dest="config_command")

    add_parser = config_sub.add_parser("add-account", help="Add or update one account")
    add_parser.add_argument("--name", required=True, help="Account name")
    add_parser.add_argument("--cookie", default="", help="Cookie value")
    add_parser.add_argument("--cookie-env", default="", help="Read cookie from env var name")
    add_parser.add_argument("--note", default="", help="Optional note")
    add_parser.add_argument("--disabled", action="store_true", help="Add account as disabled")
    add_parser.add_argument("--prompt-cookie", action="store_true", help="Prompt cookie in terminal input")

    interactive_add_parser = config_sub.add_parser("add-account-interactive", help="Add account interactively")
    interactive_add_parser.add_argument("--disabled", action="store_true", help="Add account as disabled")

    config_sub.add_parser("list-accounts", help="List configured accounts")
    validate_parser = config_sub.add_parser("validate-cookies", help="Validate enabled account cookies")
    validate_parser.add_argument("--all", action="store_true", help="Include disabled accounts")

    review_parser = config_sub.add_parser("resolve-manual-review", help="Resolve pending manual review items")
    review_parser.add_argument(
        "--mode",
        default="reject_and_ignore",
        choices=["reject_and_ignore"],
        help="How to resolve pending manual review items",
    )

    return parser.parse_args()


def _normalize_task_names(raw: str) -> list[str]:
    names = [name.strip() for name in str(raw or "").split(",") if name.strip()]
    invalid = [name for name in names if name not in TASKS]
    if invalid:
        raise ValueError(f"Unknown task(s): {', '.join(invalid)}")
    if not names:
        raise ValueError("At least one task is required")
    return names


def _resolve_cookie(args: argparse.Namespace) -> str:
    if args.cookie:
        return str(args.cookie)
    if args.cookie_env:
        value = os.environ.get(str(args.cookie_env), "")
        if value:
            return value
    if args.prompt_cookie:
        return getpass.getpass("Cookie: ").strip()
    return ""


def _save_account(args: argparse.Namespace) -> int:
    store = AccountStore()
    accounts = store.load_all()
    cookie = _resolve_cookie(args)
    if not cookie:
        _log("添加账号失败：cookie 为空，请使用 --cookie / --cookie-env / --prompt-cookie")
        return 1

    name = str(args.name or "").strip()
    if not name:
        _log("添加账号失败：name 不能为空")
        return 1

    enabled = not bool(args.disabled)
    note = str(args.note or "")
    existing = next((acc for acc in accounts if acc.name == name), None)
    if existing is None:
        accounts.append(Account(name=name, cookie=cookie, note=note, enabled=enabled))
        action = "added"
    else:
        existing.cookie = cookie
        existing.note = note
        existing.enabled = enabled
        action = "updated"

    store.save_all(accounts)
    _log(f"Account {action}: {name} (enabled={enabled})")
    return 0


def _save_account_interactive(args: argparse.Namespace) -> int:
    name = input("Account name: ").strip()
    if not name:
        _log("添加账号失败：name 不能为空")
        return 1
    note = input("Note (optional): ").strip()
    cookie = getpass.getpass("Cookie: ").strip()
    if not cookie:
        _log("添加账号失败：cookie 不能为空")
        return 1
    payload = argparse.Namespace(
        name=name,
        cookie=cookie,
        cookie_env="",
        note=note,
        disabled=bool(getattr(args, "disabled", False)),
        prompt_cookie=False,
    )
    return _save_account(payload)


def _resolve_pending_manual_review(mode: str, runtime: AgentRuntime | None = None) -> int:
    ctx = runtime or AgentRuntime()
    items = ctx.imported_store.get_all()
    pending_ids = [
        item.product_id
        for item in items
        if str(getattr(item, "manual_review_state", "") or "").strip().lower() == MANUAL_REVIEW_STATE_PENDING
        or str(getattr(item, "op_status", "") or "").strip() == "待确认"
    ]
    if not pending_ids:
        _log("待确认处理: 无待确认商品")
        return 0

    if mode != "reject_and_ignore":
        _log(f"不支持的 mode: {mode}")
        return 1

    result = apply_manual_review_batch_decision(
        ctx.account_store,
        ctx.imported_store,
        pending_ids,
        MANUAL_REVIEW_ACTION_REJECT_AND_IGNORE,
        on_progress=_log,
    )
    ok = int(result.get("ok") or 0)
    fail = int(result.get("fail") or 0)
    _log(f"待确认处理完成: total={len(pending_ids)}, ok={ok}, fail={fail}, mode={mode}")
    return 0 if fail == 0 else 2


def _list_accounts() -> int:
    store = AccountStore()
    accounts = store.load_all()
    if not accounts:
        _log("No accounts configured")
        return 0
    _log(f"Accounts: {len(accounts)}")
    for acc in accounts:
        cookie_preview = f"{(acc.cookie or '')[:12]}..." if acc.cookie else "<empty>"
        _log(f"- {acc.name} | enabled={acc.enabled} | cookie={cookie_preview} | note={acc.note or '-'}")
    return 0


def _validate_account_cookie(account: Account) -> tuple[bool, str]:
    try:
        svc = ImeiService(account.name, account.cookie)
        ok, reason = svc.check_cookie_valid()
    except Exception as exc:
        return False, str(exc)
    return bool(ok), str(reason or "")


def _validate_cookies(*, include_disabled: bool = False) -> dict[str, object]:
    store = AccountStore()
    accounts = store.load_all()
    if not include_disabled:
        accounts = [acc for acc in accounts if acc.enabled]

    passed: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []

    for acc in accounts:
        if not acc.cookie:
            skipped.append(f"{acc.name}: empty cookie")
            continue
        ok, reason = _validate_account_cookie(acc)
        if ok:
            passed.append(acc.name)
        else:
            failed.append(f"{acc.name}: {reason or 'invalid cookie'}")

    summary = {
        "total": len(accounts),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
    }
    return summary


def _print_validation_summary(summary: dict[str, object]) -> None:
    total = int(summary.get("total") or 0)
    passed = list(summary.get("passed") or [])
    failed = list(summary.get("failed") or [])
    skipped = list(summary.get("skipped") or [])
    _log(
        f"Cookie validation done: total={total}, pass={len(passed)}, fail={len(failed)}, skip={len(skipped)}"
    )
    for name in passed:
        _log(f"  PASS {name}")
    for line in failed:
        _log(f"  FAIL {line}")
    for line in skipped:
        _log(f"  SKIP {line}")


def run_loop(tasks: list[str], *, interval_seconds: int, once: bool, max_cycles: int) -> int:
    runtime = AgentRuntime()
    preflight = _validate_cookies(include_disabled=False)
    _print_validation_summary(preflight)
    if not preflight.get("passed"):
        _log("No valid enabled accounts. Exit.")
        return 1

    cycles = 0
    interval = max(int(interval_seconds or 1), 1)
    cap = max(int(max_cycles or 0), 0)
    _log(f"Agent runner started. tasks={tasks}, interval={interval}s, once={once}, max_cycles={cap}")

    while True:
        cycles += 1
        _log(f"Cycle {cycles} started")
        task_rows: list[list[str]] = []
        persisted_rows: list[list[str]] = []

        for name in tasks:
            fn = TASKS[name]
            _log(f"Task {name} started")
            try:
                result = fn(runtime)
                task_rows.append(_build_task_summary_row(name, result))
                persisted_rows.extend(_collect_persisted_rows(name, result))
            except Exception as exc:
                error_text = str(exc or "unknown error")
                _log(f"Task {name} failed: {error_text}")
                task_rows.append(_build_task_summary_row(name, None, error=error_text))

        _log("Cycle task summary:")
        _log(
            "\n" + _format_table(
                ["Task", "Total", "OK", "Skip", "Fail", "ManualReview", "Persisted", "Note"],
                task_rows,
            )
        )

        if persisted_rows:
            _log("Cycle repricing records:")
            _log(
                "\n" + _format_table(
                    ["Task", "Account", "Item", "Old", "New", "Diff", "Trigger"],
                    persisted_rows,
                )
            )
        else:
            _log("Cycle repricing records: no persisted price changes")

        _log("Cycle post-process: resolve manual review (reject_and_ignore)")
        post_review_code = _resolve_pending_manual_review("reject_and_ignore", runtime=runtime)
        if post_review_code != 0:
            _log(f"Cycle post-process warning: resolve-manual-review exit={post_review_code}")

        _log(f"Cycle {cycles} finished")

        if once:
            return 0
        if cap > 0 and cycles >= cap:
            return 0
        next_time = datetime.datetime.now() + datetime.timedelta(seconds=interval)
        _log(f"Cycle {cycles} idle: waiting {interval}s, next cycle at {next_time.strftime('%H:%M:%S')}")
        time.sleep(interval)


def main() -> int:
    args = _parse_args()

    if args.command in (None, "run"):
        task_names = _normalize_task_names(getattr(args, "tasks", "erp_sync,auto_reprice"))
        return run_loop(
            task_names,
            interval_seconds=int(getattr(args, "interval_seconds", 300) or 300),
            once=bool(getattr(args, "once", False)),
            max_cycles=int(getattr(args, "max_cycles", 0) or 0),
        )

    if args.command == "config":
        sub = getattr(args, "config_command", None)
        if sub == "add-account":
            return _save_account(args)
        if sub == "add-account-interactive":
            return _save_account_interactive(args)
        if sub == "list-accounts":
            return _list_accounts()
        if sub == "validate-cookies":
            summary = _validate_cookies(include_disabled=bool(getattr(args, "all", False)))
            _print_validation_summary(summary)
            return 0 if len(summary.get("failed") or []) == 0 else 2
        if sub == "resolve-manual-review":
            return _resolve_pending_manual_review(str(getattr(args, "mode", "reject_and_ignore")))
        _log("Unknown config subcommand")
        return 1

    _log(f"Unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
