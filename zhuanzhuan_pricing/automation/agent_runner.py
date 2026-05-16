# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import datetime
import getpass
import hashlib
import json
import os
import select
import socket
import subprocess
import sys
import threading
import time
import shlex
import signal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable


AUTH_CONFIG_FILE = Path(__file__).resolve().parents[2] / "config" / "agent_auth.json"
RMB_TO_USD_RATE = float(os.getenv("AGENT_RMB_TO_USD_RATE", "7.2"))

import requests

from ..config import cfg
from ..core.models import Account
from ..core.rule_engine import RuleEngine
from ..core.batch_store import BatchItemStore
from ..services.data_store import AccountStore, SoldCache, CostPriceMap
from ..services.erp_service import ErpConfig
from ..services.notify_service import FeishuRobotNotifier, WxAppClient, WxAppConfig, WxRobotNotifier
from ..services.mysql_store import get_mysql_store
from ..services.feishu_inventory_service import FeishuInventoryService
from ..services.zhuanzhuan_api import ImeiService
from ..post_qc_posterior_v2.service import check_single_item_intercept
from .tasks import (
    MANUAL_REVIEW_ACTION_REJECT,
    MANUAL_REVIEW_ACTION_REJECT_AND_IGNORE,
    MANUAL_REVIEW_STATE_PENDING,
    apply_manual_review_batch_decision,
    apply_manual_review_decision,
    task_auto_list,
    task_auto_reprice,
    task_erp_sync,
    task_probe_perturbation,
    task_sales_report,
    task_stale_drop,
)
from ..tools.migrate_local_to_mysql import migrate as migrate_local_to_mysql


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
        self.imported_pool_file = str(_runtime_dir() / "imported_items_pool.json")
        self.wx_app_config = WxAppConfig()
        self.wx_app_client = WxAppClient(self.wx_app_config)
        self.wx_robot_notifier = WxRobotNotifier(str(self.wx_app_config.get("robot_webhook", "") or ""))
        self.feishu_robot_notifier = FeishuRobotNotifier(str(self.wx_app_config.get("feishu_webhook", "") or ""))
        restored = self.imported_store.load_from_file(self.imported_pool_file)
        _log(f"导入商品池恢复: {restored} 条（{self.imported_pool_file}）")


TaskFunc = Callable[[AgentRuntime], dict | str]


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _fmt_money(value) -> str:
    try:
        return f"${float(value):.0f}"
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
            str(sample.get("model") or "-"),
            str(sample.get("condition") or "-"),
            str(sample.get("qc_code") or "-"),
            str(sample.get("imei") or "-"),
            _fmt_money(sample.get("settle_price")),
            str(sample.get("changed_at") or "-"),
            str(sample.get("trigger") or "-"),
        ])
    return rows


def _collect_risk_bucket(task_name: str, result: dict | str | None) -> dict[str, int]:
    if not isinstance(result, dict):
        return {}
    raw = result.get("risk_bucket")
    if not isinstance(raw, dict):
        return {}
    merged: dict[str, int] = {}
    for key, value in raw.items():
        label = str(key or "none").strip() or "none"
        merged[label] = merged.get(label, 0) + _to_int(value, 0)
    return merged


def _risk_source_label(source: str) -> str:
    mapping = {
        "none": "无风险标签",
        "low_sample": "样本不足",
        "below_target_profit": "低于目标利润",
        "below_cost": "低于成本",
        "direction_lock": "方向锁定",
        "cooldown": "冷却期",
        "oscillation": "价格震荡",
        "official_deviation": "官方参考价偏离",
    }
    key = str(source or "none").strip() or "none"
    return mapping.get(key, key)


def _build_risk_summary_rows(risk_stats: dict[str, dict[str, int]]) -> list[list[str]]:
    rows: list[list[str]] = []
    for source in sorted(risk_stats.keys(), key=lambda x: (-risk_stats[x].get("count", 0), x)):
        stat = risk_stats[source]
        rows.append([
            _risk_source_label(source),
            str(_to_int(stat.get("count"), 0)),
            str(_to_int(stat.get("auto_passed"), 0)),
            str(_to_int(stat.get("manual_review"), 0)),
            str(_to_int(stat.get("skipped"), 0)),
        ])
    return rows


def _risk_natural_summary(risk_stats: dict[str, dict[str, int]]) -> str:
    if not risk_stats:
        return "本轮无风险标签数据"
    ranked = sorted(
        [(source, _to_int(data.get("count"), 0)) for source, data in risk_stats.items()],
        key=lambda kv: (-kv[1], kv[0]),
    )
    if not ranked:
        return "本轮无风险标签数据"
    top = ranked[0]
    top_label = _risk_source_label(top[0])
    if len(ranked) == 1:
        return f"本轮风险主要来源：{top_label}（{top[1]} 条）"
    second = ranked[1]
    second_label = _risk_source_label(second[0])
    return f"本轮风险主要来源：{top_label}（{top[1]} 条），其次 {second_label}（{second[1]} 条）"


def _wechat_cycle_report_enabled() -> bool:
    from ..config import cfg
    return bool(cfg.get("report_wechat_enabled", False))


def _snapshot_inventory_state(runtime: AgentRuntime) -> dict[str, tuple[str, str, str]]:
    state: dict[str, tuple[str, str, str]] = {}
    for item in runtime.imported_store.get_all():
        pid = str(getattr(item, "product_id", "") or "").strip()
        if not pid:
            continue
        state[pid] = (
            str(getattr(item, "account_name", "") or "-"),
            str(getattr(getattr(item, "status", None), "label", getattr(item, "status", "未知")) or "未知"),
            str(getattr(item, "qc_code", "") or pid),
        )
    return state


def _collect_inventory_changes(before: dict[str, tuple[str, str, str]], after: dict[str, tuple[str, str, str]]) -> list[list[str]]:
    rows: list[list[str]] = []
    for pid, after_data in after.items():
        if pid not in before:
            continue
        before_data = before[pid]
        if before_data[1] == after_data[1]:
            continue
        rows.append([after_data[0], after_data[2], before_data[1], after_data[1]])
    return rows


def _collect_turnover_metrics(runtime: AgentRuntime, target_date: datetime.date | None = None) -> dict[str, float | int | list[dict[str, str]]]:
    today = target_date or datetime.date.today()
    owned_product_ids = {
        str(getattr(item, "product_id", "") or "").strip()
        for item in runtime.imported_store.get_all()
        if str(getattr(item, "product_id", "") or "").strip()
    }
    sold_records = runtime.sold_cache.load()
    owned_today_records = []
    for row in sold_records:
        sold_time = getattr(row, "sold_time", None)
        if not sold_time or sold_time.date() != today:
            continue
        if str(getattr(row, "product_id", "") or "").strip() not in owned_product_ids:
            continue
        owned_today_records.append(row)

    latest_record_by_pid: dict[str, object] = {}
    for row in sorted(owned_today_records, key=lambda x: getattr(x, "sold_time", datetime.datetime.min), reverse=True):
        pid = str(getattr(row, "product_id", "") or "").strip()
        if not pid or pid in latest_record_by_pid:
            continue
        latest_record_by_pid[pid] = row

    unique_today_records = list(latest_record_by_pid.values())
    sold_today = len(unique_today_records)
    _log(
        "动销口径核对："
        f"缓存总数 {len(sold_records)}，"
        f"今日+ERP池原始条数 {len(owned_today_records)}，"
        f"按product_id去重后 {sold_today}"
    )
    on_sale_count = len(runtime.imported_store.on_sale_snapshot())
    denominator = sold_today + on_sale_count
    turnover_rate = (sold_today / denominator) if denominator > 0 else 0.0
    sales_detail: list[dict[str, str]] = []
    for row in unique_today_records:
        sold_price = "-"
        settle_price = "-"
        try:
            sold_price = f"{float(getattr(row, 'sold_price', 0.0) or 0.0):.0f}"
        except Exception:
            sold_price = "-"
        try:
            settle_raw = getattr(row, "settle_price", None)
            if settle_raw is not None:
                settle_price = f"{float(settle_raw):.0f}"
        except Exception:
            settle_price = "-"
        sales_detail.append(
            {
                "product_id": str(getattr(row, "product_id", "") or "-"),
                "model": str(getattr(row, "model", "") or "-"),
                "condition": str(getattr(row, "condition", "") or "-"),
                "sold_price": sold_price,
                "settle_price": settle_price,
                "sold_time": getattr(row, "sold_time", datetime.datetime.min).strftime("%H:%M") if getattr(row, "sold_time", None) else "-",
            }
        )
    return {
        "sold_today": sold_today,
        "on_sale_count": on_sale_count,
        "turnover_rate": turnover_rate,
        "sales_detail": sales_detail,
    }


def _network_preflight(timeout_seconds: float = 1.5) -> tuple[bool, str]:
    endpoints = [("api.zhuanzhuan.com", 443), ("open.feishu.cn", 443)]
    for host, port in endpoints:
        try:
            with socket.create_connection((host, port), timeout=timeout_seconds):
                continue
        except Exception as exc:
            return False, f"{host}:{port} unreachable ({exc})"
    return True, "ok"


def _build_cycle_report_message(*, cycle: int, task_rows: list[list[str]], persisted_rows: list[list[str]], risk_stats: dict[str, dict[str, int]], risk_summary_text: str, inventory_change_rows: list[list[str]], turnover_metrics: dict[str, float | int]) -> str:
    total_ok = sum(_to_int(row[2], 0) for row in task_rows if len(row) >= 3)
    total_skip = sum(_to_int(row[3], 0) for row in task_rows if len(row) >= 4)
    total_fail = sum(_to_int(row[4], 0) for row in task_rows if len(row) >= 5)
    total_manual = sum(_to_int(row[5], 0) for row in task_rows if len(row) >= 6)
    total_persisted = len(persisted_rows)
    sold_today = _to_int(turnover_metrics.get("sold_today"), 0)
    on_sale_count = _to_int(turnover_metrics.get("on_sale_count"), 0)
    turnover_rate = float(turnover_metrics.get("turnover_rate") or 0.0)
    lines = [
        f"📊 Agent 轮次汇总 #{cycle}",
        f"执行结果：成功 {total_ok}，跳过 {total_skip}，失败 {total_fail}",
        f"待确认：{total_manual}，改价写入：{total_persisted}",
        f"今日销售：{sold_today}，在架数量：{on_sale_count}，动销率：{turnover_rate:.2%}",
        risk_summary_text,
    ]

    if task_rows:
        lines.append("任务明细:")
        for row in task_rows[:8]:
            if len(row) < 8:
                continue
            lines.append(
                f"- {row[0]}: total={row[1]} ok={row[2]} skip={row[3]} fail={row[4]} manual={row[5]} persisted={row[6]}"
            )

    if persisted_rows:
        lines.append("改价明细(全量):")
        for row in persisted_rows:
            if len(row) < 13:
                continue
            lines.append(
                f"- [{row[0]}] [{row[1]}] 型号:{row[6]} 成色:{row[7]} 时间:{row[11]}"
                f"\n  标识 qc:{row[8]} imei:{row[9]}"
                f"\n  价格 {row[3]}→{row[4]} ({row[5]}) 到手:{row[10]} trigger:{row[12]}"
            )

    sales_detail = list(turnover_metrics.get("sales_detail") or [])
    if sales_detail:
        lines.append(f"今日销售明细：{len(sales_detail)} 台")
        for idx, row in enumerate(sales_detail, start=1):
            lines.append(
                f"{idx}. [{row.get('product_id', '-')}] 型号:{row.get('model', '-')} 成色:{row.get('condition', '-')} 成交:{row.get('sold_price', '-')} 到手:{row.get('settle_price', '-')} 时间:{row.get('sold_time', '-')}"
            )
    else:
        lines.append("今日销售明细：0 台")

    if risk_stats:
        top = sorted(risk_stats.items(), key=lambda kv: (-_to_int(kv[1].get("count"), 0), kv[0]))[:3]
        lines.append("风险Top:")
        for idx, (source, data) in enumerate(top, start=1):
            lines.append(f"{idx}. {source}: {_to_int(data.get('count'), 0)}")
    if inventory_change_rows:
        lines.append(f"库存状态变更：{len(inventory_change_rows)} 条")
        for idx, row in enumerate(inventory_change_rows[:10], start=1):
            lines.append(f"{idx}. [{row[0]}] [{row[1]}] {row[2]}→{row[3]}")
        if len(inventory_change_rows) > 10:
            lines.append(f"… 其余 {len(inventory_change_rows) - 10} 条省略")
    else:
        lines.append("库存状态变更：0 条")
    return "\n".join(lines)


def _runtime_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "runtime"


def _cloud_sync_state_file() -> Path:
    return _runtime_dir() / "cloud_sync_state.json"


def _posterior_backfill_checkpoint_file() -> Path:
    return _runtime_dir() / "posterior_backfill_checkpoint.json"


def _load_posterior_backfill_checkpoint() -> dict:
    path = _posterior_backfill_checkpoint_file()
    if not path.exists():
        return {"done_slices": []}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        done = data.get("done_slices") if isinstance(data, dict) else []
        if not isinstance(done, list):
            done = []
        return {"done_slices": [str(x) for x in done]}
    except Exception:
        return {"done_slices": []}


def _save_posterior_backfill_checkpoint(state: dict) -> None:
    path = _posterior_backfill_checkpoint_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _load_cloud_sync_state() -> dict:
    path = _cloud_sync_state_file()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cloud_sync_state(state: dict) -> None:
    try:
        runtime_dir = _runtime_dir()
        runtime_dir.mkdir(parents=True, exist_ok=True)
        with _cloud_sync_state_file().open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        return


def _cloud_sync_interval_seconds() -> int:
    raw = cfg.get("cloud_sync_interval_seconds", 12 * 60 * 60)
    try:
        value = int(raw)
    except Exception:
        value = 12 * 60 * 60
    return max(value, 60)


def _should_run_cloud_sync(kind: str) -> tuple[bool, int]:
    state = _load_cloud_sync_state()
    now_ts = int(time.time())
    interval = _cloud_sync_interval_seconds()
    last_ts = 0
    try:
        last_ts = int((state.get(kind) or {}).get("last_ts") or 0)
    except Exception:
        last_ts = 0
    if last_ts <= 0:
        return True, 0
    elapsed = max(now_ts - last_ts, 0)
    remaining = max(interval - elapsed, 0)
    return remaining <= 0, remaining


def _mark_cloud_sync_ran(kind: str) -> None:
    state = _load_cloud_sync_state()
    bucket = state.get(kind)
    if not isinstance(bucket, dict):
        bucket = {}
    bucket["last_ts"] = int(time.time())
    bucket["last_at"] = _now_text()
    state[kind] = bucket
    _save_cloud_sync_state(state)


def _load_agent_auth_config() -> dict:
    if not AUTH_CONFIG_FILE.exists():
        return {"auth_required": False, "users": []}
    try:
        with AUTH_CONFIG_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"auth_required": False, "users": []}
        users = data.get("users")
        if not isinstance(users, list):
            users = []
        return {
            "auth_required": bool(data.get("auth_required", False)),
            "users": users,
            "feishu_usage_app_token": str(data.get("feishu_usage_app_token") or "").strip(),
            "feishu_usage_table_id": str(data.get("feishu_usage_table_id") or "").strip(),
            "feishu_posterior_app_token": str(data.get("feishu_posterior_app_token") or "").strip(),
            "feishu_posterior_table_id": str(data.get("feishu_posterior_table_id") or "").strip(),
            "remote_auth_enabled": bool(data.get("remote_auth_enabled", False)),
            "feishu_auth_app_id": str(data.get("feishu_auth_app_id") or "").strip(),
            "feishu_auth_app_secret": str(data.get("feishu_auth_app_secret") or "").strip(),
            "feishu_auth_app_token": str(data.get("feishu_auth_app_token") or "").strip(),
            "feishu_auth_table_id": str(data.get("feishu_auth_table_id") or "").strip(),
        }
    except Exception:
        return {"auth_required": False, "users": []}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _verify_agent_access_local(*, user_id: str, access_key: str, cfg: dict) -> tuple[bool, str]:
    uid = str(user_id or "").strip()
    key = str(access_key or "")
    if not uid or not key:
        return False, "missing --agent-user-id or --agent-access-key"

    users = list(cfg.get("users") or [])
    target = None
    for row in users:
        if not isinstance(row, dict):
            continue
        if str(row.get("user_id") or "").strip() != uid:
            continue
        if not bool(row.get("enabled", True)):
            return False, f"user disabled: {uid}"
        target = row
        break

    if target is None:
        return False, f"user not found: {uid}"

    expect_hash = str(target.get("key_hash") or "").strip().lower()
    if not expect_hash:
        return False, f"user key hash missing: {uid}"
    actual_hash = _sha256_text(key)
    if actual_hash != expect_hash:
        return False, f"invalid access key for user: {uid}"
    return True, "ok-local"


def _verify_agent_access_remote(*, user_id: str, access_key: str, cfg: dict) -> tuple[bool, str]:
    uid = str(user_id or "").strip()
    key = str(access_key or "")
    if not uid or not key:
        return False, "missing --agent-user-id or --agent-access-key"

    app_id = str(cfg.get("feishu_auth_app_id") or "").strip()
    app_secret = str(cfg.get("feishu_auth_app_secret") or "").strip()
    app_token = str(cfg.get("feishu_auth_app_token") or "").strip()
    table_id = str(cfg.get("feishu_auth_table_id") or "").strip()
    if not app_id or not app_secret or not app_token or not table_id:
        return False, "remote auth config missing"

    tenant_token = _feishu_tenant_token(app_id, app_secret)
    if not tenant_token:
        return False, "remote auth token empty"

    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    headers = {"Authorization": f"Bearer {tenant_token}", "Content-Type": "application/json"}
    page_token = ""
    expected = _sha256_text(key)

    while True:
        params = {"page_size": 200}
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        data = resp.json() if resp.content else {}
        if int(data.get("code", -1)) != 0:
            return False, f"remote auth query failed: {data}"
        payload = data.get("data") or {}
        for item in list(payload.get("items") or []):
            fields = item.get("fields") or {}
            row_uid = str(fields.get("user_id") or fields.get("用户ID") or "").strip()
            if row_uid != uid:
                continue
            enabled = str(fields.get("enabled") or fields.get("启用") or "true").strip().lower()
            if enabled in {"0", "false", "no", "禁用"}:
                return False, f"user disabled: {uid}"
            row_hash = str(fields.get("access_key_hash") or fields.get("key_hash") or fields.get("密钥哈希") or "").strip().lower()
            if not row_hash:
                return False, f"remote key hash missing: {uid}"
            if row_hash != expected:
                return False, f"invalid access key for user: {uid}"
            return True, "ok-remote"
        if not bool(payload.get("has_more", False)):
            break
        page_token = str(payload.get("page_token") or "")
        if not page_token:
            break
    return False, f"user not found: {uid}"


def _verify_agent_access(*, user_id: str, access_key: str) -> tuple[bool, str]:
    cfg = _load_agent_auth_config()
    if not bool(cfg.get("auth_required", False)):
        return True, "auth disabled"

    if bool(cfg.get("remote_auth_enabled", False)):
        remote_ok, remote_reason = _verify_agent_access_remote(user_id=user_id, access_key=access_key, cfg=cfg)
        if remote_ok:
            return True, remote_reason
        local_ok, local_reason = _verify_agent_access_local(user_id=user_id, access_key=access_key, cfg=cfg)
        if local_ok:
            return True, local_reason
        return False, f"remote={remote_reason}; local={local_reason}"

    return _verify_agent_access_local(user_id=user_id, access_key=access_key, cfg=cfg)


def _intercept_state_file() -> Path:
    return _runtime_dir() / "post_qc_intercept_state.json"


def _posterior_state_file() -> Path:
    return _runtime_dir() / "posterior_intercept_state.json"


def _load_intercept_state(path: Path | None = None) -> dict:
    state_path = path or _intercept_state_file()
    if not state_path.exists():
        return {"written_keys": []}
    try:
        with state_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        keys = data.get("written_keys") if isinstance(data, dict) else []
        if not isinstance(keys, list):
            keys = []
        return {"written_keys": [str(x) for x in keys]}
    except Exception:
        return {"written_keys": []}


def _save_intercept_state(state: dict, path: Path | None = None) -> None:
    state_path = path or _intercept_state_file()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _intercept_row_key(row: dict) -> str:
    qc_code = str(row.get("qc_code") or "").strip()
    imei = str(row.get("imei") or "").strip()
    identity = qc_code or imei
    event_time = str(row.get("event_time") or row.get("apply_return_time") or row.get("sold_time") or "").strip()
    day = event_time[:10] if len(event_time) >= 10 else event_time
    account = str(row.get("account_name") or "").strip()
    base = "|".join([account, identity, day])
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def _split_reason_clauses(qc_item: str, qc_reason: str) -> list[str]:
    item = str(qc_item or "").strip() or "-"
    reason = str(qc_reason or "").strip()
    if not reason:
        return [f"{item} / -"]
    clauses: list[str] = []
    for part in [x.strip() for x in reason.split(",") if x.strip()]:
        if ":" in part:
            sub_item, sub_val = [x.strip() for x in part.split(":", 1)]
            clauses.append(f"{sub_item or item} / {sub_val or '-'}")
        else:
            clauses.append(f"{item} / {part}")
    return clauses or [f"{item} / {reason}"]


def _is_valid_reason_clause(clause: str) -> bool:
    text = str(clause or "").strip()
    if not text:
        return False
    if " / -" in text or text.endswith("/-") or text.endswith("/ -"):
        return False

    lower_ignores = (
        "清洁问题 / 有脏污",
        "充电次数 /",
        "是否有拆机 / 浅拆",
    )
    if any(x in text for x in lower_ignores):
        return False

    filtered_values = ("正常", "无", "未检出", "几乎不可见")
    if any(text.endswith(f"/ {v}") for v in filtered_values):
        return False
    if "/ 未检出" in text:
        return False
    if "/ 无维修" in text:
        return False
    if "碎裂" in text:
        return True

    strong_marks = ("异常", "更换", "压伤", "泛黄", "泛红", "残影", "画面异常", "退回", "拦截")
    if any(x in text for x in strong_marks):
        return True

    if "拆" in text and "浅拆" not in text:
        return True
    if "脏污" in text and "清洁问题" not in text:
        return True
    return False


def _posterior_reason_text(row: dict) -> str:
    clauses = _extract_reason_clauses_from_row(row)
    if clauses:
        return "；".join(clauses)
    primary = str(row.get("reason_hint") or row.get("status_detail") or "").strip()
    if primary:
        return primary
    fallback = str(row.get("manual_review_reason") or row.get("reprice_msg") or "").strip()
    if fallback:
        return fallback
    return "状态回退：已售→未上架（原因待补充）"


def _normalize_event_time_text(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or raw == "-":
        return "-"
    candidates = [raw]
    if "T" in raw:
        candidates.append(raw.replace("T", " "))
    if len(raw) >= 19:
        candidates.append(raw[:19])
    if len(raw) >= 10:
        candidates.append(raw[:10])
    for text in candidates:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.datetime.strptime(text, fmt)
                if fmt == "%Y-%m-%d":
                    return dt.strftime("%Y-%m-%d 00:00:00")
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
    return raw


def _posterior_event_time_text(row: dict) -> str:
    event_time = str(
        row.get("event_time")
        or row.get("apply_return_time")
        or row.get("sold_time")
        or _now_text()
    ).strip()
    return _normalize_event_time_text(event_time)


def _posterior_row_key(row: dict) -> str:
    reason = _posterior_reason_text(row)
    reason_hash = hashlib.sha1(reason.encode("utf-8")).hexdigest()
    identity = str(row.get("product_id") or "").strip() or str(row.get("qc_code") or "").strip()
    base = "|".join([
        identity,
        str(row.get("old_status") or "").strip(),
        str(row.get("new_status") or "").strip(),
        _posterior_event_time_text(row)[:10],
        reason_hash,
    ])
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def _posterior_image_text(row: dict) -> str:
    photos = list(row.get("flawed_photos") or [])
    for value in photos:
        text = str(value or "").strip()
        if text:
            return text
    for key in ("photo", "image", "image_url", "photo_url"):
        text = str(row.get(key) or "").strip()
        if text:
            return text
    return "-"


def _build_posterior_bitable_record(row: dict) -> dict:
    old_status = str(row.get("old_status") or "-").strip() or "-"
    new_status = str(row.get("new_status") or "-").strip() or "-"
    status_change = f"{old_status}→{new_status}"
    dedup_key = str(row.get("_dedup_key") or _posterior_row_key(row)).strip()
    fields = {
        "事件类型": str(row.get("event_type") or "status_regression"),
        "状态变化": status_change,
        "拦截原因": _posterior_reason_text(row),
        "账号": str(row.get("account_name") or "-"),
        "质检码": str(row.get("qc_code") or "-"),
        "商品ID": str(row.get("product_id") or "-"),
        "型号": str(row.get("model") or row.get("title") or "-"),
        "IMEI": str(row.get("imei") or "-"),
        "原始状态文本": str(row.get("status_name") or row.get("status_detail") or "-"),
        "事件时间": _posterior_event_time_text(row),
        "来源通道": str(row.get("source_channel") or row.get("source") or "unknown"),
        "拦截图片": _posterior_image_text(row),
        "去重键": dedup_key,
    }
    return {"fields": fields}


def _write_posterior_intercepts_to_bitable(*, app_id: str, app_secret: str, app_token: str, table_id: str, rows: list[dict], ignore_state_dedup: bool = False) -> tuple[int, int, str]:
    state = _load_intercept_state(_posterior_state_file())
    written_keys = set(state.get("written_keys") or [])
    new_rows: list[dict] = []
    for row in rows:
        key = _posterior_row_key(row)
        if (not ignore_state_dedup) and key in written_keys:
            continue
        row["_dedup_key"] = key
        new_rows.append(row)
    if not new_rows:
        return 0, len(rows), "no new rows"
    tenant_token = _feishu_tenant_token(app_id, app_secret)
    if not tenant_token:
        return 0, len(rows), "tenant token empty"

    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"
    headers = {"Authorization": f"Bearer {tenant_token}", "Content-Type": "application/json"}

    created = 0
    batch_size = 100
    for i in range(0, len(new_rows), batch_size):
        chunk = new_rows[i:i + batch_size]
        payload = {"records": [_build_posterior_bitable_record(row) for row in chunk]}
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        data = resp.json() if resp.content else {}
        if int(data.get("code", -1)) != 0:
            return created, len(rows), f"bitable failed: {data}"
        created += len(chunk)
        for row in chunk:
            key = str(row.get("_dedup_key") or "").strip()
            if key:
                written_keys.add(key)

    state["written_keys"] = sorted(written_keys)
    _save_intercept_state(state, _posterior_state_file())
    return created, len(rows), "ok"


def _extract_reason_clauses_from_row(row: dict) -> list[str]:
    clauses: list[str] = []
    qc_item = str(row.get("qc_item_name") or "").strip()
    post_reason = str(row.get("post_qc_result") or "").strip()
    if qc_item or post_reason:
        clauses.extend(_split_reason_clauses(qc_item, post_reason))
    reason_hint = str(row.get("reason_hint") or "").strip()
    if reason_hint:
        for part in [x.strip() for x in reason_hint.split("；") if x.strip()]:
            clauses.append(part)
    deduped: list[str] = []
    for c in clauses:
        if c not in deduped and _is_valid_reason_clause(c):
            deduped.append(c)
    return deduped


def _is_abnormal_reason_text(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    if ":" in raw and "," in raw:
        clauses = _split_reason_clauses("", raw)
        return any(_is_valid_reason_clause(c) for c in clauses)
    return _is_valid_reason_clause(raw)


def _is_posterior_status_regression(old_status: str, new_status: str, status_text: str = "") -> bool:
    old_s = str(old_status or "").strip()
    new_s = str(new_status or "").strip()
    text = str(status_text or "").strip()
    if old_s == "已售" and new_s == "未上架":
        return True
    if "已退回" in (old_s, new_s) or "退回" in text:
        return True
    return False


def _extract_posterior_intercept_rows(status_records: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for row in status_records:
        old_status = str(row.get("old_status") or "").strip()
        new_status = str(row.get("new_status") or "").strip()
        status_text = str(row.get("status_name") or row.get("status_detail") or "").strip()
        if not _is_posterior_status_regression(old_status, new_status, status_text):
            continue
        payload = dict(row)
        payload["event_type"] = str(payload.get("event_type") or "status_regression")
        payload["is_post_intercept"] = True
        payload["source_channel"] = str(payload.get("source_channel") or "status_refresh")
        rows.append(payload)
    return rows


def _posterior_bitable_target(runtime: AgentRuntime) -> tuple[str, str, str, str]:
    auth_cfg = _load_agent_auth_config()
    app_id = str(runtime.wx_app_config.get("feishu_app_id", "") or auth_cfg.get("feishu_auth_app_id") or "").strip()
    app_secret = str(runtime.wx_app_config.get("feishu_app_secret", "") or auth_cfg.get("feishu_auth_app_secret") or "").strip()
    app_token = str(auth_cfg.get("feishu_posterior_app_token") or auth_cfg.get("feishu_usage_app_token") or runtime.wx_app_config.get("feishu_app_token", "") or "").strip()
    table_id = str(auth_cfg.get("feishu_posterior_table_id") or auth_cfg.get("feishu_usage_table_id") or runtime.wx_app_config.get("feishu_table_id", "") or "").strip()
    return app_id, app_secret, app_token, table_id


def _enrich_posterior_rows_with_images(runtime: AgentRuntime, rows: list[dict]) -> None:
    account_cookie_map = {
        str(acc.name or "").strip(): str(acc.cookie or "").strip()
        for acc in runtime.account_store.enabled_accounts()
        if str(acc.name or "").strip() and str(acc.cookie or "").strip()
    }
    svc_cache: dict[str, ImeiService] = {}
    diff_cache: dict[tuple[str, str, str], str] = {}

    for row in rows:
        if _posterior_image_text(row) != "-":
            continue
        account_name = str(row.get("account_name") or "").strip()
        qc_code = str(row.get("qc_code") or "").strip()
        product_id = str(row.get("product_id") or "").strip()
        if not account_name or (not qc_code and not product_id):
            continue
        key = (account_name, qc_code, product_id)
        cached = diff_cache.get(key)
        if cached is not None:
            if cached:
                row["flawed_photos"] = [cached]
            continue
        cookie = account_cookie_map.get(account_name, "")
        if not cookie:
            diff_cache[key] = ""
            continue
        svc = svc_cache.get(account_name)
        if svc is None:
            svc = ImeiService(account_name, cookie)
            svc_cache[account_name] = svc
        photo = ""
        try:
            diff_items = svc.query_post_qc_diff(qc_code=qc_code, product_id=product_id)
            for item in diff_items:
                photos = [str(x).strip() for x in list(item.get("flawedPhotos") or []) if str(x).strip()]
                if photos:
                    photo = photos[0]
                    break
        except Exception:
            photo = ""
        diff_cache[key] = photo
        if photo:
            row["flawed_photos"] = [photo]


def _query_post_qc_rows_for_status_changes(runtime: AgentRuntime, status_records: list[dict]) -> list[dict]:
    account_cookie_map = {
        str(acc.name or "").strip(): str(acc.cookie or "").strip()
        for acc in runtime.account_store.enabled_accounts()
        if str(acc.name or "").strip() and str(acc.cookie or "").strip()
    }
    svc_cache: dict[str, ImeiService] = {}
    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    for record in status_records:
        old_status = str(record.get("old_status") or "").strip()
        new_status = str(record.get("new_status") or "").strip()
        status_text = str(record.get("status_name") or record.get("status_detail") or "").strip()
        if not _is_posterior_status_regression(old_status, new_status, status_text):
            continue

        account_name = str(record.get("account_name") or "").strip()
        qc_code = str(record.get("qc_code") or "").strip()
        product_id = str(record.get("product_id") or "").strip()
        if not account_name or (not qc_code and not product_id):
            continue

        dedup_key = (account_name, qc_code, product_id)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        cookie = account_cookie_map.get(account_name, "")
        if not cookie:
            continue
        svc = svc_cache.get(account_name)
        if svc is None:
            svc = ImeiService(account_name, cookie)
            svc_cache[account_name] = svc

        try:
            diff_items = svc.query_post_qc_diff(qc_code=qc_code, product_id=product_id)
        except Exception:
            diff_items = []

        if not diff_items:
            payload = dict(record)
            payload["event_type"] = "status_regression"
            payload["source_channel"] = str(payload.get("source_channel") or "status_refresh")
            payload["reason_hint"] = _posterior_reason_text(payload)
            rows.append(payload)
            continue

        for diff in diff_items:
            post_result = str(diff.get("postQcResult") or "").strip()
            qc_item_name = str(diff.get("qcItemName") or "").strip()
            if not _is_abnormal_reason_text(f"{qc_item_name}:{post_result}"):
                continue
            payload = dict(record)
            payload["event_type"] = "post_qc_intercept"
            payload["source_channel"] = str(payload.get("source_channel") or "status_refresh")
            payload["qc_item_name"] = qc_item_name
            payload["ori_qc_result"] = str(diff.get("oriQcResult") or "").strip()
            payload["post_qc_result"] = post_result
            payload["flawed_photos"] = [str(x).strip() for x in list(diff.get("flawedPhotos") or []) if str(x).strip()]
            payload["reason_hint"] = _posterior_reason_text(payload)
            rows.append(payload)

    return _aggregate_posterior_rows_daily(rows)


def _write_immediate_posterior_intercepts(runtime: AgentRuntime, status_records: list[dict]) -> tuple[int, int, str]:
    rows = _query_post_qc_rows_for_status_changes(runtime, status_records)
    if not rows:
        return 0, 0, "no posterior status regressions"
    app_id, app_secret, app_token, table_id = _posterior_bitable_target(runtime)
    if not app_id or not app_secret or not app_token or not table_id:
        return 0, len(rows), "posterior bitable config missing"
    return _write_posterior_intercepts_to_bitable(
        app_id=app_id,
        app_secret=app_secret,
        app_token=app_token,
        table_id=table_id,
        rows=rows,
    )


def _aggregate_posterior_rows_daily(rows: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], dict] = {}
    for row in rows:
        status_text = str(row.get("status_name") or row.get("status_detail") or "").strip()
        old_status = str(row.get("old_status") or "").strip()
        new_status = str(row.get("new_status") or "").strip()
        if not _is_posterior_status_regression(old_status, new_status, status_text):
            continue

        reason_clauses = _extract_reason_clauses_from_row(row)
        if not reason_clauses:
            continue

        product_id = str(row.get("product_id") or "").strip()
        qc_code = str(row.get("qc_code") or "").strip()
        identity = product_id or qc_code
        if not identity:
            continue

        day = _posterior_event_time_text(row)[:10]
        key = (identity, day)
        slot = grouped.get(key)
        if slot is None:
            payload = dict(row)
            payload["_reason_set"] = set(reason_clauses)
            grouped[key] = payload
            continue

        reason_set = slot.get("_reason_set")
        if not isinstance(reason_set, set):
            reason_set = set()
            slot["_reason_set"] = reason_set
        reason_set.update(reason_clauses)

        slot_photo = _posterior_image_text(slot)
        row_photo = _posterior_image_text(row)
        if slot_photo == "-" and row_photo != "-":
            slot["flawed_photos"] = [row_photo]

        slot_event = _posterior_event_time_text(slot)
        row_event = _posterior_event_time_text(row)
        if row_event > slot_event:
            slot["event_time"] = row.get("event_time") or row.get("apply_return_time") or row.get("sold_time")
            slot["apply_return_time"] = row.get("apply_return_time") or slot.get("apply_return_time")
            slot["sold_time"] = row.get("sold_time") or slot.get("sold_time")
            slot["new_status"] = str(row.get("new_status") or slot.get("new_status") or "")
            slot["status_name"] = str(row.get("status_name") or slot.get("status_name") or "")

    result: list[dict] = []
    for payload in grouped.values():
        reason_set = payload.pop("_reason_set", set())
        if isinstance(reason_set, set) and reason_set:
            payload["reason_hint"] = "；".join(sorted(reason_set))
        else:
            payload["reason_hint"] = _posterior_reason_text(payload)
        result.append(payload)
    return result


def _build_posterior_rows_from_post_qc(rows: list[dict]) -> list[dict]:
    result: list[dict] = []
    for row in rows:
        payload = dict(row)
        payload["event_type"] = "post_qc_intercept"
        payload["source_channel"] = str(payload.get("source_channel") or "post_qc").strip() or "post_qc"
        payload["old_status"] = str(payload.get("old_status") or "已售")
        payload["new_status"] = str(payload.get("new_status") or payload.get("status_name") or "未上架")
        payload["reason_hint"] = _posterior_reason_text(payload)
        result.append(payload)
    return _aggregate_posterior_rows_daily(result)


def _fetch_post_qc_intercepts(runtime: AgentRuntime, days: int = 2) -> list[dict]:
    pool_items = list(runtime.imported_store.get_all())
    if not pool_items:
        _log("后验拦截过滤: 商品池为空，返回0条")
        return []

    pool_by_account: dict[str, list] = {}
    for item in pool_items:
        account_name = str(getattr(item, "account_name", "") or "").strip()
        if not account_name:
            continue
        pool_by_account.setdefault(account_name, []).append(item)

    rows_by_key: dict[str, dict] = {}

    def _is_intercept_row(row: dict) -> bool:
        clauses = _split_reason_clauses(
            str(row.get("qc_item_name") or "").strip(),
            str(row.get("post_qc_result") or "").strip(),
        )
        return any(_is_valid_reason_clause(c) for c in clauses)

    def add_row(row: dict) -> None:
        if not _is_intercept_row(row):
            return
        key = _intercept_row_key(row)
        rows_by_key[key] = row

    for account in runtime.account_store.enabled_accounts():
        name = str(getattr(account, "name", "") or "").strip()
        cookie = str(getattr(account, "cookie", "") or "").strip()
        if not name or not cookie:
            continue
        svc = ImeiService(name, cookie)

        # 轨道A：列表抓取
        try:
            for row in svc.fetch_post_qc_intercepts(days=days):
                add_row(row)
        except Exception as exc:
            _log(f"抓取后验拦截失败 [{name}]: {exc}")

        # 轨道B：商品池定向补采，避免列表漏单
        for pool_item in pool_by_account.get(name, []):
            product_id = str(getattr(pool_item, "product_id", "") or "").strip()
            qc_code = str(getattr(pool_item, "qc_code", "") or "").strip()
            if not product_id and not qc_code:
                continue
            detail = None
            try:
                if qc_code:
                    detail = svc.query_by_qc_code(qc_code)
            except Exception:
                detail = None
            if detail is None and product_id:
                try:
                    detail = svc._query_product_by_id(product_id, statuses=("1", "80", "60", "0"))
                except Exception:
                    detail = None
            if detail is None:
                continue

            lifecycle_sold = ""
            lifecycle_apply = ""
            try:
                body = svc._merchant_product_list({
                    "pageNum": 1,
                    "pageSize": 20,
                    "tagIds": [],
                    "noTagIds": [],
                    "labels": [],
                    "productIds": [str(getattr(detail, "product_id", "") or product_id)],
                    "statusList": ["1"],
                    "salesInShop": False,
                })
                items = (body.get("respData") or body.get("data") or {}).get("list") or []
                if items:
                    lc = (items[0].get("lifecycleTimes") or {})
                    lifecycle_sold = str(lc.get("soldTime") or "")
                    lifecycle_apply = str(lc.get("applyReturnTime") or "")
            except Exception:
                pass

            effective_product_id = str(getattr(detail, "product_id", "") or product_id)
            effective_qc_code = str(getattr(detail, "qc_code", "") or qc_code)
            diff_items = svc.query_post_qc_diff(qc_code=effective_qc_code, product_id=effective_product_id)
            for diff in diff_items:
                post_result = str(diff.get("postQcResult") or "").strip()
                if not post_result:
                    continue
                if any(flag in post_result for flag in ("正常", "无", "未检出", "几乎不可见")) and not any(
                    bad in post_result for bad in ("异常", "拆", "更换", "压伤", "泛黄", "泛红", "残影", "脏污", "轻微", "细微", "画面")
                ):
                    continue
                add_row({
                    "account_name": name,
                    "site": "YY",
                    "qc_code": effective_qc_code,
                    "imei": str(getattr(detail, "imei", "") or ""),
                    "title": str(getattr(detail, "title", "") or ""),
                    "model": str(getattr(detail, "model", "") or ""),
                    "sold_time": lifecycle_sold,
                    "apply_return_time": lifecycle_apply,
                    "event_time": lifecycle_apply or lifecycle_sold,
                    "status_name": str(getattr(getattr(detail, "status", None), "label", "") or ""),
                    "qc_item_name": str(diff.get("qcItemName") or ""),
                    "ori_qc_result": str(diff.get("oriQcResult") or ""),
                    "post_qc_result": post_result,
                    "flawed_photos": list(diff.get("flawedPhotos") or []),
                    "product_id": effective_product_id,
                })

    rows = list(rows_by_key.values())

    # 仅保留商品池内
    pool_product_ids = {
        str(getattr(item, "product_id", "") or "").strip()
        for item in pool_items
        if str(getattr(item, "product_id", "") or "").strip()
    }
    filtered = [
        row for row in rows
        if str(row.get("product_id") or "").strip() in pool_product_ids
    ]
    _log(f"后验拦截过滤: 原始 {len(rows)} 条 -> 商品池内 {len(filtered)} 条")
    return filtered


def _row_day(row: dict) -> datetime.date | None:
    raw = str(row.get("event_time") or row.get("apply_return_time") or row.get("sold_time") or "").strip()
    if not raw:
        return None
    dt = None
    try:
        dt = datetime.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except Exception:
        try:
            dt = datetime.datetime.fromisoformat(raw)
        except Exception:
            dt = None
    return dt.date() if dt else None


def _build_intercept_summary_by_days(rows: list[dict], targets: list[datetime.date]) -> tuple[dict, dict]:
    raw_buckets: dict[str, list[dict]] = {d.isoformat(): [] for d in targets}
    target_set = set(targets)
    for row in rows:
        day = _row_day(row)
        if day in target_set:
            raw_buckets[day.isoformat()].append(row)

    # 按“账号+qc+日期”聚合成每机每天一条，只保留差异项摘要
    buckets: dict[str, list[dict]] = {}
    summary: dict[str, dict] = {}

    for d in targets:
        key = d.isoformat()
        merged: dict[str, dict] = {}
        for row in raw_buckets[key]:
            qc = str(row.get("qc_code") or "").strip()
            account = str(row.get("account_name") or "").strip()
            group_key = f"{account}|{qc}|{key}"
            reason_piece = f"{row.get('qc_item_name') or '-'}:{row.get('post_qc_result') or '-'}"
            photos = [str(x) for x in (row.get("flawed_photos") or []) if str(x).strip()]
            entry = merged.get(group_key)
            if entry is None:
                merged[group_key] = {
                    "account_name": account,
                    "model": row.get("model") or row.get("title") or "-",
                    "qc_code": qc or "-",
                    "imei": row.get("imei") or "-",
                    "event_time": row.get("event_time") or row.get("apply_return_time") or row.get("sold_time") or "-",
                    "reasons": [reason_piece],
                    "photo": photos[0] if photos else "-",
                }
                continue
            if reason_piece not in entry["reasons"]:
                entry["reasons"].append(reason_piece)
            if entry.get("photo") in ("", "-") and photos:
                entry["photo"] = photos[0]

        machine_rows = list(merged.values())
        # 最严格去重：同一qc同一天只留一条（优先有图，再取时间较新）
        by_qc: dict[str, dict] = {}
        for row in machine_rows:
            qc_key = str(row.get("qc_code") or "-")
            prev = by_qc.get(qc_key)
            if prev is None:
                by_qc[qc_key] = row
                continue
            prev_has_photo = str(prev.get("photo") or "-") not in ("", "-")
            curr_has_photo = str(row.get("photo") or "-") not in ("", "-")
            if curr_has_photo and not prev_has_photo:
                by_qc[qc_key] = row
                continue
            prev_time = str(prev.get("event_time") or "")
            curr_time = str(row.get("event_time") or "")
            if curr_time > prev_time:
                by_qc[qc_key] = row
        machine_rows = list(by_qc.values())
        buckets[key] = machine_rows

        reason_count: dict[str, int] = {}
        for item in machine_rows:
            for reason in item.get("reasons") or []:
                reason_count[reason] = reason_count.get(reason, 0) + 1
        top_reasons = sorted(reason_count.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        summary[key] = {"count": len(machine_rows), "top_reasons": top_reasons}

    return summary, buckets


def _format_intercept_summary_message(summary: dict, buckets: dict[str, list[dict]], targets: list[datetime.date]) -> str:
    lines = ["后验拦截汇总（仅差异项，按机器去重）"]
    for d in targets:
        key = d.isoformat()
        data = summary.get(key) or {}
        rows = list(buckets.get(key) or [])
        lines.append(f"{key} 拦截机器: {int(data.get('count') or 0)} 台")
        for idx, (reason, count) in enumerate(data.get("top_reasons") or [], start=1):
            lines.append(f"  差异Top{idx}: {reason} ({count})")
        if rows:
            lines.append(f"{key} 机器明细:")
            for idx, row in enumerate(rows, start=1):
                reason_text = "；".join(list(row.get("reasons") or [])[:5])
                lines.append(
                    f"{idx}. 型号:{row.get('model') or '-'} "
                    f"qc:{row.get('qc_code') or '-'} "
                    f"原因:{reason_text} 图片:{row.get('photo') or '-'}"
                )
        else:
            lines.append(f"{key} 机器明细: 无")
    return "\n".join(lines)


def _build_bitable_record(row: dict) -> dict:
    photos = [str(x) for x in (row.get("flawed_photos") or []) if str(x).strip()]
    reason = f"{row.get('qc_item_name') or '-'} / {row.get('post_qc_result') or '-'}"
    event_time = _normalize_event_time_text(str(row.get("event_time") or row.get("apply_return_time") or row.get("sold_time") or "-"))
    fields = {
        "事件类型": "post_qc_intercept",
        "状态变化": f"{row.get('old_status') or '已售'}→{row.get('new_status') or '未上架'}",
        "拦截原因": reason,
        "账号": str(row.get("account_name") or "-"),
        "质检码": str(row.get("qc_code") or "-"),
        "商品ID": str(row.get("product_id") or "-"),
        "型号": str(row.get("model") or row.get("title") or "-"),
        "IMEI": str(row.get("imei") or "-"),
        "事件时间": event_time,
        "来源通道": str(row.get("source_channel") or "post_qc"),
        "拦截图片": photos[0] if photos else "-",
        "去重键": str(row.get("_dedup_key") or _intercept_row_key(row)),
    }
    return {"fields": fields}


def _write_intercepts_to_bitable(*, app_id: str, app_secret: str, app_token: str, table_id: str, rows: list[dict]) -> tuple[int, int, str]:
    current_rows: dict[str, dict] = {}
    for row in rows:
        key = _intercept_row_key(row)
        row["_dedup_key"] = key
        current_rows[key] = row

    tenant_token = _feishu_tenant_token(app_id, app_secret)
    if not tenant_token:
        return 0, len(rows), "tenant token empty"

    base_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    headers = {"Authorization": f"Bearer {tenant_token}", "Content-Type": "application/json"}

    # 读取现有记录（去重键 -> record_id）
    existing: dict[str, str] = {}
    page_token = ""
    while True:
        params = {
            "page_size": 500,
            "field_names": json.dumps(["去重键"], ensure_ascii=False),
        }
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(base_url, headers=headers, params=params, timeout=15)
        data = resp.json() if resp.content else {}
        if int(data.get("code", -1)) != 0:
            return 0, len(rows), f"bitable list failed: {data}"
        payload = data.get("data") or {}
        for item in payload.get("items") or []:
            record_id = str(item.get("record_id") or "").strip()
            fields = item.get("fields") or {}
            dedup_key = str(fields.get("去重键") or "").strip()
            if record_id and dedup_key:
                existing[dedup_key] = record_id
        if not payload.get("has_more"):
            break
        page_token = str(payload.get("page_token") or "").strip()
        if not page_token:
            break

    created = 0
    updated = 0
    deleted = 0

    # upsert：在当前拦截集合中的记录一律覆盖
    to_create = [row for key, row in current_rows.items() if key not in existing]
    to_update = [(existing[key], row) for key, row in current_rows.items() if key in existing]

    batch_size = 100
    for i in range(0, len(to_create), batch_size):
        chunk = to_create[i:i + batch_size]
        payload = {"records": [_build_bitable_record(row) for row in chunk]}
        resp = requests.post(f"{base_url}/batch_create", headers=headers, json=payload, timeout=15)
        data = resp.json() if resp.content else {}
        if int(data.get("code", -1)) != 0:
            return created, len(rows), f"bitable create failed: {data}"
        created += len(chunk)

    for i in range(0, len(to_update), batch_size):
        chunk = to_update[i:i + batch_size]
        payload = {
            "records": [
                {
                    "record_id": record_id,
                    "fields": _build_bitable_record(row)["fields"],
                }
                for record_id, row in chunk
            ]
        }
        resp = requests.post(f"{base_url}/batch_update", headers=headers, json=payload, timeout=15)
        data = resp.json() if resp.content else {}
        if int(data.get("code", -1)) != 0:
            return created + updated, len(rows), f"bitable update failed: {data}"
        updated += len(chunk)

    # 删除不在本轮拦截集合中的旧记录
    stale_record_ids = [record_id for key, record_id in existing.items() if key not in current_rows]
    for i in range(0, len(stale_record_ids), batch_size):
        chunk = stale_record_ids[i:i + batch_size]
        payload = {"records": chunk}
        resp = requests.post(f"{base_url}/batch_delete", headers=headers, json=payload, timeout=15)
        data = resp.json() if resp.content else {}
        if int(data.get("code", -1)) != 0:
            return created + updated, len(rows), f"bitable delete failed: {data}"
        deleted += len(chunk)

    state = _load_intercept_state()
    state["written_keys"] = sorted(current_rows.keys())
    _save_intercept_state(state)
    return created + updated, len(rows), f"ok(created={created}, updated={updated}, deleted={deleted})"


def _parse_target_dates(raw: str) -> list[datetime.date]:
    text = str(raw or "").strip().lower()
    today = datetime.date.today()
    if not text or text in {"today", "今日"}:
        return [today]
    if text in {"yesterday", "昨日"}:
        return [today - datetime.timedelta(days=1)]
    if text in {"today,yesterday", "yesterday,today", "今日,昨日", "昨日,今日"}:
        return [today - datetime.timedelta(days=1), today]
    dates: list[datetime.date] = []
    for part in [x.strip() for x in text.split(",") if x.strip()]:
        try:
            dates.append(datetime.datetime.strptime(part, "%Y-%m-%d").date())
        except Exception:
            continue
    return dates or [today]


def _build_post_qc_message_and_write(runtime: AgentRuntime, report_dates: str, app_id: str, app_secret: str, app_token: str, table_id: str, force_posterior_rewrite: bool = False) -> tuple[str, int, int, str]:
    target_dates = _parse_target_dates(report_dates)
    fetch_days = max((datetime.date.today() - min(target_dates)).days + 1, 1)
    rows = _fetch_post_qc_intercepts(runtime, days=fetch_days)
    summary, buckets = _build_intercept_summary_by_days(rows, target_dates)

    created = 0
    scanned = len(rows)
    write_note = "skip write"
    if app_id and app_secret and app_token and table_id:
        created, scanned, write_note = _write_intercepts_to_bitable(
            app_id=app_id,
            app_secret=app_secret,
            app_token=app_token,
            table_id=table_id,
            rows=rows,
        )

    posterior_created = 0
    posterior_scanned = 0
    posterior_note = "skip posterior write"
    posterior_app_id, posterior_app_secret, posterior_app_token, posterior_table_id = _posterior_bitable_target(runtime)
    posterior_rows = _build_posterior_rows_from_post_qc(rows)
    if posterior_rows:
        posterior_rows = sorted(posterior_rows, key=lambda x: _posterior_event_time_text(x))
        posterior_scanned = len(posterior_rows)
        if posterior_app_id and posterior_app_secret and posterior_app_token and posterior_table_id:
            posterior_created, posterior_scanned, posterior_note = _write_posterior_intercepts_to_bitable(
                app_id=posterior_app_id,
                app_secret=posterior_app_secret,
                app_token=posterior_app_token,
                table_id=posterior_table_id,
                rows=posterior_rows,
                ignore_state_dedup=bool(force_posterior_rewrite),
            )
        else:
            posterior_note = "posterior bitable config missing"

    message = _format_intercept_summary_message(summary, buckets, target_dates)
    note = f"{write_note}; posterior_created={posterior_created}, posterior_scanned={posterior_scanned}, posterior_note={posterior_note}"
    return message, created, scanned, note


def _run_posterior_backfill_pool(runtime: AgentRuntime, args: argparse.Namespace) -> int:
    pool_product_ids = {
        str(getattr(item, "product_id", "") or "").strip()
        for item in runtime.imported_store.get_all()
        if str(getattr(item, "product_id", "") or "").strip()
    }
    if not pool_product_ids:
        _log("ERP池补录：商品池为空，无需执行")
        return 0

    days_max = max(int(getattr(args, "days", 3) or 3), 1)
    timeout_seconds = max(int(getattr(args, "slice_timeout_seconds", 45) or 45), 5)
    use_checkpoint = not bool(getattr(args, "ignore_checkpoint", False))

    checkpoint = _load_posterior_backfill_checkpoint() if use_checkpoint else {"done_slices": []}
    done_slices = set(checkpoint.get("done_slices") or [])

    accounts = list(runtime.account_store.enabled_accounts())
    total_slices = len(accounts) * days_max
    current_slice = 0

    all_filtered_rows: list[dict] = []
    timeout_slices = 0
    failed_slices = 0
    success_slices = 0
    skipped_slices = 0

    class _SliceTimeout(Exception):
        pass

    def _timeout_handler(signum, frame):
        raise _SliceTimeout("slice timeout")

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _timeout_handler)

    try:
        for account in accounts:
            name = str(getattr(account, "name", "") or "").strip()
            cookie = str(getattr(account, "cookie", "") or "").strip()
            if not name or not cookie:
                continue

            svc = ImeiService(name, cookie)
            for days in range(1, days_max + 1):
                current_slice += 1
                slice_key = f"{name}|days={days}"
                if use_checkpoint and slice_key in done_slices:
                    skipped_slices += 1
                    _log(f"[slice {current_slice}/{total_slices}] 跳过已完成分片：{slice_key}")
                    continue

                fetched_rows: list[dict] = []
                try:
                    signal.alarm(timeout_seconds)
                    fetched_rows = svc.fetch_post_qc_intercepts(days=days, page_size=10)
                    signal.alarm(0)
                    success_slices += 1
                except _SliceTimeout:
                    signal.alarm(0)
                    timeout_slices += 1
                    _log(f"[slice {current_slice}/{total_slices}] 超时：{slice_key}，timeout={timeout_seconds}s")
                    continue
                except Exception as exc:
                    signal.alarm(0)
                    failed_slices += 1
                    _log(f"[slice {current_slice}/{total_slices}] 失败：{slice_key}，error={exc}")
                    continue

                dedup_seen: set[tuple[str, str, str, str, str]] = set()
                filtered_rows: list[dict] = []
                for row in fetched_rows:
                    pid = str(row.get("product_id") or "").strip()
                    if not pid or pid not in pool_product_ids:
                        continue
                    uniq = (
                        pid,
                        str(row.get("qc_code") or "").strip(),
                        str(row.get("qc_item_name") or "").strip(),
                        str(row.get("post_qc_result") or "").strip(),
                        str(row.get("event_time") or row.get("apply_return_time") or row.get("sold_time") or "").strip(),
                    )
                    if uniq in dedup_seen:
                        continue
                    dedup_seen.add(uniq)
                    filtered_rows.append(row)

                all_filtered_rows.extend(filtered_rows)
                _log(
                    f"[slice {current_slice}/{total_slices}] 完成：{slice_key} fetched={len(fetched_rows)} pool_filtered={len(filtered_rows)}"
                )

                if use_checkpoint:
                    done_slices.add(slice_key)
                    _save_posterior_backfill_checkpoint({"done_slices": sorted(done_slices)})

    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)

    merged_rows: dict[str, dict] = {}
    for row in all_filtered_rows:
        key = _intercept_row_key(row)
        merged_rows[key] = row
    final_rows = list(merged_rows.values())

    posterior_rows = _build_posterior_rows_from_post_qc(final_rows)
    before_img = sum(1 for row in posterior_rows if _posterior_image_text(row) != "-")
    if not bool(getattr(args, "skip_image_enrich", False)):
        _enrich_posterior_rows_with_images(runtime, posterior_rows)
    after_img = sum(1 for row in posterior_rows if _posterior_image_text(row) != "-")
    img_filled = max(after_img - before_img, 0)
    img_missing = max(len(posterior_rows) - after_img, 0)

    app_id, app_secret, app_token, table_id = _posterior_bitable_target(runtime)
    if not app_id or not app_secret or not app_token or not table_id:
        _log("ERP池补录：posterior bitable config missing")
        return 1

    created, scanned, note = _write_posterior_intercepts_to_bitable(
        app_id=app_id,
        app_secret=app_secret,
        app_token=app_token,
        table_id=table_id,
        rows=posterior_rows,
    )

    _log(
        "ERP池补录汇总："
        f" slices_total={total_slices} success={success_slices} timeout={timeout_slices} fail={failed_slices} skipped={skipped_slices}"
        f" pool_rows={len(final_rows)} write_created={created} write_scanned={scanned}"
        f" img_filled={img_filled} img_missing={img_missing} note={note}"
    )
    return 0 if "failed" not in str(note).lower() else 2


def _run_post_qc_report(runtime: AgentRuntime, args: argparse.Namespace) -> int:
    app_id = str(getattr(args, "feishu_app_id", "") or "").strip()
    app_secret = str(getattr(args, "feishu_app_secret", "") or "").strip()
    app_token = str(getattr(args, "feishu_app_token", "") or "").strip()
    table_id = str(getattr(args, "feishu_table_id", "") or "").strip()
    report_dates = str(getattr(args, "report_dates", "today,yesterday") or "today,yesterday")

    message, created, scanned, write_note = _build_post_qc_message_and_write(
        runtime,
        report_dates,
        app_id,
        app_secret,
        app_token,
        table_id,
        force_posterior_rewrite=bool(getattr(args, "force_posterior_rewrite", False)),
    )

    pushed, reason = runtime.feishu_robot_notifier.send_text(message)
    _log(f"后验拦截汇总发送结果：{'已发送' if pushed else '发送失败'}（{reason}）")
    _log(f"后验拦截写表结果：新增 {created}，扫描 {scanned}，备注：{write_note}")
    try:
        get_mysql_store().write_post_qc_intercepts(_fetch_post_qc_intercepts(runtime, days=2))
    except Exception:
        pass

    app_id2, app_secret2, app_token2, table_id2 = _usage_bitable_target(runtime, args)
    usage_ok, usage_reason = _write_usage_to_bitable(
        app_id=app_id2,
        app_secret=app_secret2,
        app_token=app_token2,
        table_id=table_id2,
        entry={
            "agent_user_id": str(getattr(args, "agent_user_id", "") or "").strip() or "anonymous",
            "ts": _now_text(),
            "command": "post-qc-report",
            "tasks": "post_qc_report",
            "ok": 1 if pushed else 0,
            "skip": 0,
            "fail": 0 if pushed else 1,
            "manual_review": 0,
            "persisted": int(created or 0),
            "turnover_rate": "-",
            "note": f"scanned={scanned} write_note={write_note}",
        },
    )
    _log(f"使用记录上报结果：{'已上传' if usage_ok else '未上传'}（{usage_reason}）")
    try:
        get_mysql_store().write_usage_log({
            "agent_user_id": str(getattr(args, "agent_user_id", "") or "").strip() or "anonymous",
            "ts": _now_text(),
            "command": "post-qc-report",
            "tasks": "post_qc_report",
            "ok": 1 if pushed else 0,
            "skip": 0,
            "fail": 0 if pushed else 1,
            "manual_review": 0,
            "persisted": int(created or 0),
            "turnover_rate": "-",
            "note": f"scanned={scanned} write_note={write_note}",
        })
    except Exception:
        pass

    _log("说明: 写表去重；汇总按你选择的日期分开统计并带文字明细。")
    return 0 if pushed else 2


def _extract_report_dates_from_text(text: str) -> str:
    raw = str(text or "").strip()
    if not raw.startswith("拦截"):
        return ""
    remain = raw[2:].strip()
    if remain in {"", "今", "今天", "today"}:
        return "today"
    if remain in {"昨", "昨天", "yesterday"}:
        return "yesterday"
    try:
        datetime.datetime.strptime(remain, "%Y-%m-%d")
        return remain
    except Exception:
        return ""


def _feishu_tenant_token(app_id: str, app_secret: str) -> str:
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    data = resp.json() if resp.content else {}
    return str(data.get("tenant_access_token") or "")


def _feishu_reply_text(tenant_token: str, open_id: str, text: str) -> tuple[bool, str]:
    url = "https://open.feishu.cn/open-apis/im/v1/messages"
    headers = {"Authorization": f"Bearer {tenant_token}", "Content-Type": "application/json"}
    payload = {
        "receive_id": open_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    resp = requests.post(url, headers=headers, params={"receive_id_type": "open_id"}, json=payload, timeout=15)
    data = resp.json() if resp.content else {}
    if int(data.get("code", -1)) == 0:
        return True, "ok"
    return False, str(data)


def _build_usage_bitable_record(entry: dict) -> dict:
    fields = {
        "用户ID": str(entry.get("agent_user_id") or "anonymous"),
        "时间": str(entry.get("ts") or _now_text()),
        "命令": str(entry.get("command") or "run"),
        "任务集": str(entry.get("tasks") or "-"),
        "成功数": _to_int(entry.get("ok"), 0),
        "失败数": _to_int(entry.get("fail"), 0),
        "跳过数": _to_int(entry.get("skip"), 0),
        "待确认数": _to_int(entry.get("manual_review"), 0),
        "改价写入数": _to_int(entry.get("persisted"), 0),
        "动销率": str(entry.get("turnover_rate") or "-"),
        "备注文本": str(entry.get("note") or ""),
    }
    return {"fields": fields}


def _write_usage_to_bitable(*, app_id: str, app_secret: str, app_token: str, table_id: str, entry: dict) -> tuple[bool, str]:
    if not app_id or not app_secret or not app_token or not table_id:
        return False, "usage bitable config missing"
    tenant_token = _feishu_tenant_token(app_id, app_secret)
    if not tenant_token:
        return False, "tenant token empty"
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"
    headers = {"Authorization": f"Bearer {tenant_token}", "Content-Type": "application/json"}
    payload = {"records": [_build_usage_bitable_record(entry)]}
    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    data = resp.json() if resp.content else {}
    if int(data.get("code", -1)) != 0:
        return False, f"usage bitable failed: {data}"
    return True, "ok"


def _usage_bitable_target(runtime: AgentRuntime, args: argparse.Namespace | None = None) -> tuple[str, str, str, str]:
    args = args or argparse.Namespace()
    auth_cfg = _load_agent_auth_config()
    app_id = str(getattr(args, "feishu_app_id", "") or runtime.wx_app_config.get("feishu_app_id", "") or "").strip()
    app_secret = str(getattr(args, "feishu_app_secret", "") or runtime.wx_app_config.get("feishu_app_secret", "") or "").strip()
    app_token = str(auth_cfg.get("feishu_usage_app_token") or getattr(args, "feishu_app_token", "") or runtime.wx_app_config.get("feishu_app_token", "") or "").strip()
    table_id = str(auth_cfg.get("feishu_usage_table_id") or getattr(args, "feishu_table_id", "") or runtime.wx_app_config.get("feishu_table_id", "") or "").strip()
    return app_id, app_secret, app_token, table_id


def _feishu_inventory_target(args: argparse.Namespace | None = None) -> tuple[str, str, str, str]:
    args = args or argparse.Namespace()
    auth_cfg = _load_agent_auth_config()
    app_id = str(getattr(args, "feishu_inventory_app_id", "") or auth_cfg.get("feishu_inventory_app_id") or "").strip()
    app_secret = str(getattr(args, "feishu_inventory_app_secret", "") or auth_cfg.get("feishu_inventory_app_secret") or "").strip()
    app_token = str(getattr(args, "feishu_inventory_app_token", "") or auth_cfg.get("feishu_inventory_app_token") or "").strip()
    table_id = str(getattr(args, "feishu_inventory_table_id", "") or auth_cfg.get("feishu_inventory_table_id") or "").strip()
    return app_id, app_secret, app_token, table_id


def _find_account_by_user_id(user_id: str) -> Account | None:
    uid = str(user_id or "").strip()
    if not uid:
        return None
    for account in AccountStore().load_all():
        if str(account.name or "").strip() == uid:
            return account
    return None


def _save_account_binding(user_id: str, app_token: str, table_id: str) -> None:
    uid = str(user_id or "").strip()
    if not uid:
        return
    store = AccountStore()
    accounts = store.load_all()
    target = None
    for row in accounts:
        if str(row.name or "").strip() == uid:
            target = row
            break
    bind = f"{app_token}:{table_id}" if app_token and table_id else ""
    if target is None:
        target = Account(name=uid, cookie="placeholder", note=f"inventory_binding={bind}", enabled=False)
        accounts.append(target)
    else:
        note = str(target.note or "")
        parts = [x for x in note.split("|") if x and not x.startswith("inventory_binding=")]
        if bind:
            parts.append(f"inventory_binding={bind}")
        target.note = "|".join(parts)
    store.save_all(accounts)


def _inventory_binding_from_account(account: Account | None) -> tuple[str, str]:
    if account is None:
        return "", ""
    note = str(getattr(account, "note", "") or "")
    marker = "inventory_binding="
    for part in note.split("|"):
        text = str(part or "").strip()
        if not text.startswith(marker):
            continue
        value = text[len(marker):]
        app_token, sep, table_id = value.partition(":")
        if sep:
            return app_token.strip(), table_id.strip()
    return "", ""


def _provision_user_inventory_binding(args: argparse.Namespace) -> int:
    user_id = str(getattr(args, "user_id", "") or getattr(args, "agent_user_id", "") or "").strip()
    app_token = str(getattr(args, "feishu_inventory_app_token", "") or "").strip()
    table_id = str(getattr(args, "feishu_inventory_table_id", "") or "").strip()
    if not user_id or not app_token or not table_id:
        _log("provision-user-inventory 失败：缺少 user_id/app_token/table_id")
        return 1
    _save_account_binding(user_id, app_token, table_id)
    _log(f"已绑定库存表：user={user_id} token={app_token} table={table_id}")
    return 0


def _show_user_inventory_binding(args: argparse.Namespace) -> int:
    user_id = str(getattr(args, "user_id", "") or getattr(args, "agent_user_id", "") or "").strip()
    if not user_id:
        _log("show-user-inventory-binding 失败：缺少 user_id")
        return 1
    account = _find_account_by_user_id(user_id)
    app_token, table_id = _inventory_binding_from_account(account)
    if not app_token or not table_id:
        _log(f"未找到用户库存绑定：{user_id}")
        return 2
    _log(f"用户库存绑定：user={user_id} token={app_token} table={table_id}")
    return 0


def _run_bitable_inventory_sync(args: argparse.Namespace, runtime: AgentRuntime | None = None) -> int:
    uid = str(getattr(args, "user_id", "") or getattr(args, "agent_user_id", "") or "").strip()
    app_id, app_secret, default_app_token, default_table_id = _feishu_inventory_target(args)
    bind_app_token = ""
    bind_table_id = ""
    if uid:
        account = _find_account_by_user_id(uid)
        bind_app_token, bind_table_id = _inventory_binding_from_account(account)
    app_token = bind_app_token or default_app_token
    table_id = bind_table_id or default_table_id

    service = FeishuInventoryService(
        app_id=app_id,
        app_secret=app_secret,
        app_token=app_token,
        table_id=table_id,
    )
    ok, reason = service.validate_config()
    if not ok:
        _log(f"bitable-sync 配置错误：{reason}")
        return 1

    rows, errors, parsed_ok, parsed_reason = service.fetch_and_parse()
    if not parsed_ok:
        _log(f"bitable-sync 失败：{parsed_reason}")
        return 1

    ctx = runtime or AgentRuntime()
    snapshot = {str(item.product_id or "").strip(): item for item in ctx.imported_store.get_all()}
    added = 0
    updated = 0
    failed = len(errors)

    from ..core.models import BatchItem
    for row in rows:
        target = snapshot.get(row.product_id)
        if target is None:
            item = BatchItem(
                product_id=row.product_id,
                qc_code=row.qc_code,
                title=row.title,
                current_price=0.0,
                status=row.status,
                account_name=uid,
                imei=row.imei,
                model="",
                condition="",
                capacity="",
                color="",
                cost_price=float(row.cost_price),
                listed_time=row.listed_time,
                import_source="bitable",
            )
            ctx.imported_store.extend([item])
            added += 1
            snapshot[row.product_id] = item
            continue
        target.title = row.title
        target.qc_code = row.qc_code
        target.imei = row.imei
        target.cost_price = float(row.cost_price)
        target.status = row.status
        target.listed_time = row.listed_time
        target.import_source = "bitable"
        if uid:
            target.account_name = uid
        updated += 1

    try:
        ctx.imported_store.save_to_file(ctx.imported_pool_file)
    except Exception as exc:
        _log(f"bitable-sync 保存导入池失败：{exc}")

    total = len(rows) + failed
    _log(f"bitable-sync 完成：total={total} ok={len(rows)} fail={failed} added={added} updated={updated}")
    if errors:
        for line in errors[:10]:
            _log(f"  行错误：{line}")
        if len(errors) > 10:
            _log(f"  其余 {len(errors) - 10} 条错误省略")
    return 0 if failed == 0 else 2


def _validate_bitable_inventory(args: argparse.Namespace) -> int:
    app_id, app_secret, app_token, table_id = _feishu_inventory_target(args)
    service = FeishuInventoryService(
        app_id=app_id,
        app_secret=app_secret,
        app_token=app_token,
        table_id=table_id,
    )
    ok, reason = service.validate_template()
    if ok:
        _log("validate-bitable-inventory 通过：模板可用")
        return 0
    _log(f"validate-bitable-inventory 失败：{reason}")
    return 1


def _run_feishu_event_listener(runtime: AgentRuntime, args: argparse.Namespace) -> int:
    app_id = str(getattr(args, "feishu_app_id", "") or runtime.wx_app_config.get("feishu_app_id", "") or "").strip()
    app_secret = str(getattr(args, "feishu_app_secret", "") or runtime.wx_app_config.get("feishu_app_secret", "") or "").strip()
    app_token = str(getattr(args, "feishu_app_token", "") or runtime.wx_app_config.get("feishu_app_token", "") or "").strip()
    table_id = str(getattr(args, "feishu_table_id", "") or runtime.wx_app_config.get("feishu_table_id", "") or "").strip()
    verification_token = str(getattr(args, "feishu_verification_token", "") or runtime.wx_app_config.get("feishu_event_verification_token", "") or "").strip()
    port = int(getattr(args, "port", 18088) or 18088)

    if not app_id or not app_secret:
        _log("feishu-listen 启动失败：缺少 feishu_app_id / feishu_app_secret")
        return 1

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            try:
                payload = json.loads(body)
            except Exception:
                payload = {}

            if verification_token:
                token = str(payload.get("token") or "")
                if token and token != verification_token:
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b"forbidden")
                    return

            challenge = payload.get("challenge")
            if challenge:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"challenge": challenge}).encode("utf-8"))
                return

            event = payload.get("event") or {}
            message = event.get("message") or {}
            sender = event.get("sender") or {}
            sender_id = ((sender.get("sender_id") or {}).get("open_id")) or ""
            text = ((message.get("content") or ""))
            try:
                text_json = json.loads(text) if text else {}
                msg_text = str(text_json.get("text") or "").strip()
            except Exception:
                msg_text = ""

            report_dates = _extract_report_dates_from_text(msg_text)
            if report_dates and sender_id:
                def _work():
                    tenant_token = _feishu_tenant_token(app_id, app_secret)
                    if not tenant_token:
                        return
                    _feishu_reply_text(tenant_token, sender_id, f"已受理：{msg_text}，正在统计...")
                    report_text, created, scanned, write_note = _build_post_qc_message_and_write(
                        runtime,
                        report_dates,
                        app_id,
                        app_secret,
                        app_token,
                        table_id,
                    )
                    _feishu_reply_text(tenant_token, sender_id, report_text)
                    usage_ok, usage_reason = _write_usage_to_bitable(
                        app_id=app_id,
                        app_secret=app_secret,
                        app_token=(str(_load_agent_auth_config().get("feishu_usage_app_token") or app_token or "").strip()),
                        table_id=(str(_load_agent_auth_config().get("feishu_usage_table_id") or table_id or "").strip()),
                        entry={
                            "agent_user_id": str(getattr(args, "agent_user_id", "") or "").strip() or "anonymous",
                            "ts": _now_text(),
                            "command": "feishu-listen",
                            "tasks": f"post_qc:{report_dates}",
                            "ok": 1,
                            "skip": 0,
                            "fail": 0,
                            "manual_review": 0,
                            "persisted": int(created or 0),
                            "turnover_rate": "-",
                            "note": f"scanned={scanned} write_note={write_note}",
                        },
                    )
                    _log(f"使用记录上报结果：{'已上传' if usage_ok else '未上传'}（{usage_reason}）")
                    _log(f"飞书事件处理完成：日期={report_dates}，新增={created}，扫描={scanned}，备注={write_note}")

                threading.Thread(target=_work, daemon=True).start()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"code":0}')

        def log_message(self, *args):
            return

    server = HTTPServer(("0.0.0.0", port), _Handler)
    _log(f"飞书监听已启动：0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("飞书监听已停止")
    finally:
        server.server_close()
    return 0


def _append_cycle_jsonl(payload: dict) -> tuple[bool, str]:
    try:
        runtime_dir = _runtime_dir()
        runtime_dir.mkdir(parents=True, exist_ok=True)
        output_file = runtime_dir / "agent_cycle_log.jsonl"
        with output_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        try:
            get_mysql_store().write_cycle_log(payload)
        except Exception:
            pass
        return True, str(output_file)
    except Exception as exc:
        return False, str(exc)


def _push_cycle_report(runtime: AgentRuntime, message: str) -> tuple[bool, str]:
    try:
        feishu_sender = getattr(runtime.feishu_robot_notifier, "send_text", None)
        if not callable(feishu_sender):
            return False, "feishu sender unavailable"
        feishu_ok, feishu_reason = feishu_sender(message)
        if feishu_ok:
            return True, "feishu robot"
        return False, f"feishu failed: {feishu_reason}"
    except Exception as exc:
        return False, str(exc)


def _build_sync_progress_logger(direction_label: str):
    phase_map = {
        "prepare": "准备数据",
        "cycle_logs": "轮次日志",
        "sold_records": "销售记录",
        "price_changes": "改价记录",
        "batch_snapshot": "商品池快照",
        "post_qc_intercepts": "后验拦截",
    }

    def _callback(stage: str, current: int, total: int, pct: float, message: str) -> None:
        stage_name = phase_map.get(str(stage or "").strip(), str(stage or "未知阶段"))
        percent = max(min(float(pct or 0.0) * 100.0, 100.0), 0.0)
        total_val = max(int(total or 0), 0)
        current_val = max(int(current or 0), 0)
        _log(f"[{direction_label}] {stage_name}: {current_val}/{total_val} ({percent:.1f}%) - {message}")

    return _callback


def _maybe_auto_sync_to_mysql(*, force_enable: bool = False, force_disable: bool = False) -> tuple[bool, int, str]:
    if force_disable:
        return False, 0, "disabled by cli"

    enabled_cfg = bool(cfg.get("mysql_auto_sync_on_run_enabled", False))
    enabled = bool(force_enable) or enabled_cfg
    if not enabled:
        return False, 0, "disabled"

    if bool(cfg.get("mysql_auto_sync_require_dual_write_enabled", True)) and not bool(cfg.get("mysql_dual_write_enabled", True)):
        return False, 0, "skipped: mysql dual write disabled"

    mode = str(cfg.get("mysql_auto_sync_on_run_mode", "best_effort") or "best_effort").strip().lower()
    if mode not in {"best_effort", "strict"}:
        mode = "best_effort"

    should_run, remaining = _should_run_cloud_sync("download")
    if not should_run and not force_enable:
        return False, 0, f"throttled: wait {remaining}s"

    _log(f"开始从云端同步数据（模式：{mode}）")
    try:
        code = int(migrate_local_to_mysql(on_progress=_build_sync_progress_logger("云端拉取")))
    except Exception as exc:
        return True, 1, f"sync exception: {exc}"

    if code == 0:
        _mark_cloud_sync_ran("download")
        return True, 0, "ok"
    return True, code, f"sync exit={code}"


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
    immediate_stats: dict[str, object] = {"created": 0, "scanned": 0, "note": "not-run"}

    single_enabled = bool(getattr(cfg, "posterior_single_item_enabled", False))
    single_max_per_cycle = max(int(getattr(cfg, "posterior_single_item_max_per_cycle", 10) or 10), 0)
    single_processed = 0
    single_written = 0
    single_seen: set[tuple[str, str, str]] = set()
    account_cookie_map = {
        str(acc.name or "").strip(): str(acc.cookie or "").strip()
        for acc in runtime.account_store.enabled_accounts()
        if str(acc.name or "").strip() and str(acc.cookie or "").strip()
    }

    def _on_status_records(status_records: list[dict]) -> None:
        created, scanned, note = _write_immediate_posterior_intercepts(runtime, status_records)
        immediate_stats["created"] = int(created)
        immediate_stats["scanned"] = int(scanned)
        immediate_stats["note"] = str(note)
        if scanned:
            _log(f"后验拦截即时回填：新增 {created}，扫描 {scanned}，备注：{note}")

    def _on_single_status_change(record: dict) -> None:
        nonlocal single_processed, single_written
        if not single_enabled:
            return
        if single_max_per_cycle > 0 and single_processed >= single_max_per_cycle:
            return
        old_status = str(record.get("old_status") or "").strip()
        new_status = str(record.get("new_status") or "").strip()
        if not old_status and not new_status:
            return

        dedup_key = (
            str(record.get("product_id") or "").strip(),
            old_status,
            new_status,
        )
        if dedup_key in single_seen:
            return
        single_seen.add(dedup_key)
        single_processed += 1

        try:
            rows, note = check_single_item_intercept(record=record, account_cookie_map=account_cookie_map)
        except Exception as exc:
            _log(f"后验单机检查失败（已忽略）: {exc}")
            return
        if not rows:
            return

        app_id, app_secret, app_token, table_id = _posterior_bitable_target(runtime)
        if not app_id or not app_secret or not app_token or not table_id:
            _log("后验单机检查命中但未写飞书：posterior bitable config missing")
            return

        try:
            created, scanned, write_note = _write_posterior_intercepts_to_bitable(
                app_id=app_id,
                app_secret=app_secret,
                app_token=app_token,
                table_id=table_id,
                rows=rows,
            )
            single_written += int(created or 0)
            if scanned:
                _log(
                    f"后验单机检查回填：新增 {created}，扫描 {scanned}，备注：{write_note}，检查备注：{note}"
                )
        except Exception as exc:
            _log(f"后验单机检查写飞书失败（已忽略）: {exc}")

    report = task_sales_report(
        runtime.account_store,
        notifier=runtime.feishu_robot_notifier,
        on_progress=_log,
        imported_store=runtime.imported_store,
        wx_app_client=None,
        on_status_records=_on_status_records,
        on_single_status_change=_on_single_status_change,
    )
    return {
        "report": report,
        "posterior_intercept_created": int(immediate_stats.get("created") or 0),
        "posterior_intercept_scanned": int(immediate_stats.get("scanned") or 0),
        "posterior_intercept_note": str(immediate_stats.get("note") or ""),
        "posterior_single_item_enabled": single_enabled,
        "posterior_single_item_processed": single_processed,
        "posterior_single_item_created": single_written,
    }


def _run_posterior_intercept(runtime: AgentRuntime):
    immediate_stats: dict[str, object] = {"created": 0, "scanned": 0, "note": "not-run"}

    def _on_status_records(status_records: list[dict]) -> None:
        created, scanned, note = _write_immediate_posterior_intercepts(runtime, status_records)
        immediate_stats["created"] = int(created)
        immediate_stats["scanned"] = int(scanned)
        immediate_stats["note"] = str(note)
        if scanned:
            _log(f"后验拦截实时同步：新增 {created}，扫描 {scanned}，备注：{note}")

    task_sales_report(
        runtime.account_store,
        notifier=runtime.feishu_robot_notifier,
        on_progress=_log,
        imported_store=runtime.imported_store,
        wx_app_client=None,
        on_status_records=_on_status_records,
    )
    return {
        "posterior_intercept_created": int(immediate_stats.get("created") or 0),
        "posterior_intercept_scanned": int(immediate_stats.get("scanned") or 0),
        "posterior_intercept_note": str(immediate_stats.get("note") or ""),
    }


def _run_probe_perturbation(runtime: AgentRuntime):
    return task_probe_perturbation(
        runtime.account_store,
        runtime.sold_cache,
        runtime.rule_engine,
        runtime.imported_store,
        on_progress=_log,
    )


TASKS: dict[str, TaskFunc] = {
    "erp_sync": _run_erp_sync,
    "auto_reprice": _run_auto_reprice,
    "stale_drop": _run_stale_drop,
    "auto_list": _run_auto_list,
    "sales_report": _run_sales_report,
    "posterior_intercept": _run_posterior_intercept,
    "probe_perturbation": _run_probe_perturbation,
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Headless agent runner for zhuanzhuan automation")
    parser.add_argument("--agent-user-id", default="", help="Agent user id for auth and telemetry")
    parser.add_argument("--agent-access-key", default="", help="Agent access key for auth")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run scheduled automation loop")
    run_parser.add_argument(
        "--tasks",
        default="erp_sync,auto_reprice,auto_list,probe_perturbation,posterior_intercept",
        help="Comma-separated task list. Available: erp_sync,auto_reprice,stale_drop,auto_list,sales_report,posterior_intercept,probe_perturbation",
    )
    run_parser.add_argument("--interval-seconds", type=int, default=300, help="Interval between cycles")
    run_parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    run_parser.add_argument("--max-cycles", type=int, default=0, help="Stop after N cycles (0 means unlimited)")
    run_parser.add_argument(
        "--post-manual-review-mode",
        default="off",
        choices=["off", "reject"],
        help="Post-cycle manual review resolution mode",
    )
    run_parser.add_argument(
        "--post-manual-review-source-filter",
        default="",
        help="Comma-separated manual_review_source filter, e.g. probe_perturbation,auto_reprice",
    )
    run_parser.add_argument(
        "--turnover-date",
        default="",
        help="Turnover statistics date in YYYY-MM-DD format (default: today)",
    )
    run_parser.add_argument("--auto-sync-mysql", action="store_true", help="Force enable auto data-sync before run loop")
    run_parser.add_argument("--no-auto-sync-mysql", action="store_true", help="Force disable auto data-sync before run loop")

    report_parser = subparsers.add_parser("post-qc-report", help="Collect post-QC intercepts, write Bitable with dedup, and push summary")
    report_parser.add_argument("--feishu-app-id", default="", help="Feishu app id")
    report_parser.add_argument("--feishu-app-secret", default="", help="Feishu app secret")
    report_parser.add_argument("--feishu-app-token", default="", help="Feishu bitable app token")
    report_parser.add_argument("--feishu-table-id", default="", help="Feishu bitable table id")
    report_parser.add_argument("--report-dates", default="today,yesterday", help="today/yesterday 或 YYYY-MM-DD,YYYY-MM-DD")
    report_parser.add_argument("--force-posterior-rewrite", action="store_true", help="Ignore posterior state dedup and rewrite matched posterior rows in event-time order")

    posterior_backfill_parser = subparsers.add_parser("posterior-backfill-pool", help="Backfill posterior intercepts for ERP imported pool with slice progress")
    posterior_backfill_parser.add_argument("--days", type=int, default=3, help="Max day windows to scan per account (1..N)")
    posterior_backfill_parser.add_argument("--slice-timeout-seconds", type=int, default=45, help="Timeout per account/day slice")
    posterior_backfill_parser.add_argument("--ignore-checkpoint", action="store_true", help="Ignore runtime checkpoint and rerun all slices")
    posterior_backfill_parser.add_argument("--skip-image-enrich", action="store_true", help="Skip slow image enrichment and only write base intercept rows")

    feishu_listen_parser = subparsers.add_parser("feishu-listen", help="Listen Feishu event callbacks and trigger post-qc report by keywords")
    feishu_listen_parser.add_argument("--port", type=int, default=18088, help="Local listen port for Feishu event callback")
    feishu_listen_parser.add_argument("--feishu-app-id", default="", help="Feishu app id")
    feishu_listen_parser.add_argument("--feishu-app-secret", default="", help="Feishu app secret")
    feishu_listen_parser.add_argument("--feishu-app-token", default="", help="Feishu bitable app token")
    feishu_listen_parser.add_argument("--feishu-table-id", default="", help="Feishu bitable table id")
    feishu_listen_parser.add_argument("--feishu-verification-token", default="", help="Feishu event verification token")

    data_sync_parser = subparsers.add_parser("data-sync", help="Upload local data to MySQL manually")
    data_sync_parser.add_argument(
        "--to",
        default="mysql",
        choices=["mysql"],
        help="Sync target. Currently only mysql is supported",
    )

    bitable_sync_parser = subparsers.add_parser("bitable-sync", help="Sync inventory from Feishu Bitable into imported pool")
    bitable_sync_parser.add_argument("--user-id", default="", help="Inventory owner user id")
    bitable_sync_parser.add_argument("--feishu-inventory-app-id", default="", help="Feishu inventory app id")
    bitable_sync_parser.add_argument("--feishu-inventory-app-secret", default="", help="Feishu inventory app secret")
    bitable_sync_parser.add_argument("--feishu-inventory-app-token", default="", help="Feishu inventory app token")
    bitable_sync_parser.add_argument("--feishu-inventory-table-id", default="", help="Feishu inventory table id")

    validate_inventory_parser = subparsers.add_parser("validate-bitable-inventory", help="Validate Feishu inventory table template")
    validate_inventory_parser.add_argument("--feishu-inventory-app-id", default="", help="Feishu inventory app id")
    validate_inventory_parser.add_argument("--feishu-inventory-app-secret", default="", help="Feishu inventory app secret")
    validate_inventory_parser.add_argument("--feishu-inventory-app-token", default="", help="Feishu inventory app token")
    validate_inventory_parser.add_argument("--feishu-inventory-table-id", default="", help="Feishu inventory table id")

    provision_inventory_parser = subparsers.add_parser("provision-user-inventory", help="Bind one user to one Feishu inventory table")
    provision_inventory_parser.add_argument("--user-id", required=True, help="Inventory owner user id")
    provision_inventory_parser.add_argument("--feishu-inventory-app-token", required=True, help="Feishu inventory app token")
    provision_inventory_parser.add_argument("--feishu-inventory-table-id", required=True, help="Feishu inventory table id")

    show_inventory_parser = subparsers.add_parser("show-user-inventory-binding", help="Show one user's Feishu inventory binding")
    show_inventory_parser.add_argument("--user-id", required=True, help="Inventory owner user id")

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

    config_sub.add_parser("open-cookie-manager", help="Open cookie manager window for account onboarding")
    config_sub.add_parser("list-accounts", help="List configured accounts")
    validate_parser = config_sub.add_parser("validate-cookies", help="Validate enabled account cookies")
    validate_parser.add_argument("--all", action="store_true", help="Include disabled accounts")

    review_parser = config_sub.add_parser("resolve-manual-review", help="Resolve pending manual review items")
    review_parser.add_argument(
        "--mode",
        default="reject",
        choices=["reject"],
        help="How to resolve pending manual review items",
    )

    interactive_review_parser = config_sub.add_parser(
        "manual-review-interactive",
        help="Interactively resolve pending manual review items with timeout",
    )
    interactive_review_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=20,
        help="Seconds to wait for each decision before fallback to UI confirmation",
    )

    return parser.parse_args()


def _parse_turnover_date(raw: str) -> datetime.date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid --turnover-date: {text} (expected YYYY-MM-DD)") from exc


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
    _log(f"账号已{('新增' if action == 'added' else '更新')}：{name}（启用={enabled}）")
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


def _pending_manual_review_items(runtime: AgentRuntime) -> list:
    items = runtime.imported_store.get_all()
    return [
        item
        for item in items
        if str(getattr(item, "manual_review_state", "") or "").strip().lower() == MANUAL_REVIEW_STATE_PENDING
        or str(getattr(item, "op_status", "") or "").strip() == "待确认"
    ]


def _can_open_manual_review_ui() -> bool:
    if sys.platform.startswith("darwin"):
        return True
    if os.name == "nt":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


_manual_review_window_proc: subprocess.Popen | None = None


def _open_manual_review_window_if_needed(runtime: AgentRuntime) -> tuple[bool, str]:
    global _manual_review_window_proc

    pending = _pending_manual_review_items(runtime)
    count = len(pending)
    if count <= 0:
        return False, "pending=0"
    if not _can_open_manual_review_ui():
        return False, f"pending={count}, headless"

    if _manual_review_window_proc is not None:
        if _manual_review_window_proc.poll() is None:
            return False, f"pending={count}, already_open"
        _manual_review_window_proc = None

    project_root = Path(__file__).resolve().parents[2]
    cmd = [
        sys.executable,
        "-m",
        "zhuanzhuan_pricing.ui_qt.manual_review_entry",
    ]
    try:
        _manual_review_window_proc = subprocess.Popen(cmd, cwd=str(project_root))
        cmd_text = " ".join(shlex.quote(part) for part in cmd)
        return True, f"pending={count}, launched: {cmd_text}"
    except Exception as exc:
        _manual_review_window_proc = None
        return False, f"pending={count}, launch_failed: {exc}"


def _parse_source_filter(raw: str) -> set[str]:
    return {
        part.strip().lower()
        for part in str(raw or "").split(",")
        if str(part or "").strip()
    }


def _filter_pending_manual_review_items(items: list, source_filter: set[str]) -> list:
    if not source_filter:
        return list(items)
    filtered = []
    for item in items:
        source = str(getattr(item, "manual_review_source", "") or "").strip().lower()
        if source in source_filter:
            filtered.append(item)
    return filtered


def _resolve_pending_manual_review(mode: str, runtime: AgentRuntime | None = None, source_filter: set[str] | None = None) -> int:
    ctx = runtime or AgentRuntime()
    pending_items = _pending_manual_review_items(ctx)
    selected_items = _filter_pending_manual_review_items(pending_items, source_filter or set())
    pending_ids = [item.product_id for item in selected_items if getattr(item, "product_id", "")]
    if not pending_ids:
        _log("待确认处理：当前无待确认商品")
        return 0

    action_by_mode = {
        "reject": MANUAL_REVIEW_ACTION_REJECT,
        "reject_and_ignore": MANUAL_REVIEW_ACTION_REJECT_AND_IGNORE,
    }
    action = action_by_mode.get(mode)
    if not action:
        _log(f"不支持的 mode: {mode}")
        return 1

    result = apply_manual_review_batch_decision(
        ctx.account_store,
        ctx.imported_store,
        pending_ids,
        action,
        on_progress=_log,
    )
    try:
        ctx.imported_store.save_to_file(ctx.imported_pool_file)
        _log(f"待确认处理后已保存导入池: {ctx.imported_store.count()} 条")
    except Exception as exc:
        _log(f"待确认处理后保存导入池失败: {exc}")
    ok = int(result.get("ok") or 0)
    fail = int(result.get("fail") or 0)
    source_note = "all" if not source_filter else ",".join(sorted(source_filter))
    _log(f"待确认处理完成: total={len(pending_ids)}, ok={ok}, fail={fail}, mode={mode}, sources={source_note}")
    return 0 if fail == 0 else 2

def _readline_with_timeout(timeout_seconds: int) -> str | None:
    wait_seconds = max(int(timeout_seconds or 0), 1)
    _log(f"等待输入（{wait_seconds}s，超时自动跳过并转 UI 人工确认）...")
    ready, _, _ = select.select([sys.stdin], [], [], wait_seconds)
    if not ready:
        return None
    line = sys.stdin.readline()
    if line is None:
        return None
    return str(line).strip()


def _action_label(action: str) -> str:
    mapping = {
        "accept": "接受并执行",
        "reject": "拒绝",
        "skip": "本条跳过",
    }
    return mapping.get(str(action or "").strip().lower(), str(action or ""))


def _manual_review_item_mark(item) -> str:
    return str(getattr(item, "qc_code", "") or getattr(item, "product_id", "") or "-")


def _manual_review_interactive(timeout_seconds: int, runtime: AgentRuntime | None = None) -> int:
    ctx = runtime or AgentRuntime()
    pending_items = _pending_manual_review_items(ctx)
    if not pending_items:
        _log("人工确认模式: 当前没有待确认商品")
        return 0

    stats = {
        "total": len(pending_items),
        "accepted": 0,
        "rejected": 0,
        "skipped": 0,
        "timeout": 0,
        "fail": 0,
    }
    rows: list[list[str]] = []
    _log(f"人工确认模式启动: 待确认 {len(pending_items)} 件")

    for index, item in enumerate(pending_items, start=1):
        product_id = str(getattr(item, "product_id", "") or "").strip()
        if not product_id:
            stats["skipped"] += 1
            rows.append([str(index), "-", "缺少 product_id", "跳过", "该商品缺少 product_id，无法确认"])
            continue

        account = str(getattr(item, "account_name", "") or "-")
        mark = _manual_review_item_mark(item)
        current_price = _fmt_money(getattr(item, "current_price", None))
        target_price = _fmt_money(getattr(item, "manual_review_target_price", None) or getattr(item, "new_price", None) or getattr(item, "suggested_price", None))
        reason = str(getattr(item, "manual_review_reason", "") or "-")
        target_action = str(getattr(item, "manual_review_target_action", "") or "change_price")

        _log(f"[{index}/{len(pending_items)}] [{account}] [{mark}] 当前价={current_price} 目标价={target_price} 动作={target_action}")
        _log(f"  待确认原因: {reason}")
        _log("  请选择: [a]accept [r]reject [s]skip（默认超时 skip）")

        raw = _readline_with_timeout(timeout_seconds)
        timed_out = raw is None
        if timed_out:
            action = "skip"
            stats["timeout"] += 1
            _log("  输入超时：本条已跳过。请到 UI 人工确认。")
        else:
            normalized = str(raw or "").strip().lower()
            action_map = {
                "a": "accept",
                "accept": "accept",
                "r": "reject",
                "reject": "reject",
                "s": "skip",
                "skip": "skip",
                "": "skip",
            }
            action = action_map.get(normalized, "skip")
            if action == "skip" and normalized not in {"", "s", "skip"}:
                _log(f"  未识别输入 '{normalized}'，已按 skip 处理。")

        if action == "skip":
            stats["skipped"] += 1
            msg = "超时跳过，请到 UI 人工确认" if timed_out else "手动跳过，请到 UI 人工确认"
            rows.append([str(index), mark, reason[:28], _action_label(action), msg])
            continue

        success, message = apply_manual_review_decision(
            ctx.account_store,
            ctx.imported_store,
            product_id,
            action,
            on_progress=_log,
        )
        if success:
            if action == "accept":
                stats["accepted"] += 1
            elif action == "reject":
                stats["rejected"] += 1
            rows.append([str(index), mark, reason[:28], _action_label(action), "成功"])
            _log(f"  执行结果: 成功（{_action_label(action)}）")
        else:
            stats["fail"] += 1
            err_text = str(message or "执行失败")
            rows.append([str(index), mark, reason[:28], _action_label(action), err_text[:48]])
            _log(f"  执行结果: 失败（{err_text}）")

    _log("人工确认处理明细:")
    _log("\n" + _format_table(["#", "Item", "Reason", "Action", "Result"], rows))

    summary_rows = [[
        str(stats["total"]),
        str(stats["accepted"]),
        str(stats["rejected"]),
        str(stats["skipped"]),
        str(stats["timeout"]),
        str(stats["fail"]),
    ]]
    _log("人工确认汇总:")
    _log("\n" + _format_table(["Total", "Accepted", "Rejected", "Skipped", "Timeout", "Fail"], summary_rows))

    _log("提示: 本次 skip/timeout 的商品可在 UI 中继续人工确认。")
    return 0 if stats["fail"] == 0 else 2


def _list_accounts() -> int:
    store = AccountStore()
    accounts = store.load_all()
    if not accounts:
        _log("当前没有配置任何账号")
        return 0
    _log(f"账号总数：{len(accounts)}")
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


def _open_cookie_manager() -> tuple[bool, str]:
    project_root = Path(__file__).resolve().parents[2]
    try:
        subprocess.Popen([sys.executable, "-m", "zhuanzhuan_pricing.main"], cwd=str(project_root))
        return True, "已打开 Cookie 管理窗口"
    except Exception as exc:
        return False, f"打开 Cookie 管理窗口失败: {exc}"


def _print_validation_summary(summary: dict[str, object]) -> None:
    total = int(summary.get("total") or 0)
    passed = list(summary.get("passed") or [])
    failed = list(summary.get("failed") or [])
    skipped = list(summary.get("skipped") or [])
    _log(
        f"账号登录检查完成：共 {total} 个，成功 {len(passed)}，失败 {len(failed)}，跳过 {len(skipped)}"
    )
    for name in passed:
        _log(f"  通过：{name}")
    for line in failed:
        _log(f"  失败：{line}")
    for line in skipped:
        _log(f"  跳过：{line}")


def _mysql_summary_tables() -> list[str]:
    return [
        "agent_cycle_logs",
        "price_changes",
        "sold_records",
        "batch_items_snapshot",
        "post_qc_intercepts",
        "agent_usage_logs",
    ]


def _collect_mysql_table_counts() -> tuple[dict[str, int], dict[str, str]]:
    store = get_mysql_store()
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}
    for table in _mysql_summary_tables():
        ok, msg, count = store.count_rows(table)
        if ok:
            counts[table] = int(count)
        else:
            errors[table] = str(msg or "unknown error")
    return counts, errors


def _build_mysql_delta_summary_message(*, cycle: int, deltas: dict[str, int], errors: dict[str, str]) -> str:
    lines = [f"📦 MySQL 增量摘要 #{cycle}"]
    total_delta = sum(max(int(v or 0), 0) for v in deltas.values())
    lines.append(f"本轮新增总计：{total_delta}")
    for table in _mysql_summary_tables():
        if table in errors:
            lines.append(f"- {table}: 查询失败 ({errors[table][:64]})")
        else:
            lines.append(f"- {table}: +{max(int(deltas.get(table, 0)), 0)}")
    if total_delta <= 0 and not errors:
        lines.append("本轮无新增数据")
    return "\n".join(lines)


def run_loop(tasks: list[str], *, interval_seconds: int, once: bool, max_cycles: int, post_manual_review_mode: str = "off", post_manual_review_source_filter: str = "", agent_user_id: str = "", turnover_date: datetime.date | None = None) -> int:
    runtime = AgentRuntime()
    preflight = _validate_cookies(include_disabled=False)
    _print_validation_summary(preflight)
    if not preflight.get("passed"):
        ok, message = _open_cookie_manager()
        _log(message)
        _log("没有可用的启用账号，请先在 Cookie 管理窗口新增账号后重试")
        return 1

    cycles = 0
    interval = max(int(interval_seconds or 1), 1)
    cap = max(int(max_cycles or 0), 0)
    source_filter = _parse_source_filter(post_manual_review_source_filter)
    source_filter_note = "all" if not source_filter else ",".join(sorted(source_filter))
    network_fail_streak = 0
    network_cooldown_until: datetime.datetime | None = None
    turnover_date_note = turnover_date.isoformat() if turnover_date else "today"
    _log(
        f"自动任务已启动：任务={tasks}，轮询={interval}s，单次运行={once}，最大轮次={cap}，"
        f"待确认处理={post_manual_review_mode}，来源范围={source_filter_note}，动销统计日期={turnover_date_note}"
    )

    mysql_baseline_counts: dict[str, int] = {}
    mysql_baseline_errors: dict[str, str] = {}
    if bool(cfg.get("mysql_delta_report_enabled", True)):
        mysql_baseline_counts, mysql_baseline_errors = _collect_mysql_table_counts()
        if mysql_baseline_errors:
            _log(f"MySQL 基线统计告警：{mysql_baseline_errors}")

    while True:
        cycles += 1
        _log(f"第 {cycles} 轮开始")
        now = datetime.datetime.now()
        if network_cooldown_until is not None and now < network_cooldown_until:
            remain = int((network_cooldown_until - now).total_seconds())
            _log(f"网络暂不可用，进入冷却等待：剩余 {remain}s")
            if once:
                return 0
            next_time = datetime.datetime.now() + datetime.timedelta(seconds=interval)
            _log(f"第 {cycles} 轮等待中：{interval}s 后继续（预计 {next_time.strftime('%H:%M:%S')}）")
            time.sleep(interval)
            continue

        network_ok, network_reason = _network_preflight(timeout_seconds=1.5)
        if not network_ok:
            network_fail_streak += 1
            _log(f"网络预检失败，本轮跳过：{network_reason}")
            if network_fail_streak >= 3:
                network_cooldown_until = datetime.datetime.now() + datetime.timedelta(minutes=5)
                _log("网络连续异常，进入 5 分钟冷却")
            if once:
                return 0
            next_time = datetime.datetime.now() + datetime.timedelta(seconds=interval)
            _log(f"第 {cycles} 轮等待中：{interval}s 后继续（预计 {next_time.strftime('%H:%M:%S')}）")
            time.sleep(interval)
            continue

        network_fail_streak = 0
        network_cooldown_until = None
        _log("网络检查通过")

        inventory_before = _snapshot_inventory_state(runtime)
        task_rows: list[list[str]] = []
        persisted_rows: list[list[str]] = []
        risk_stats: dict[str, dict[str, int]] = {}

        for name in tasks:
            fn = TASKS[name]
            _log(f"任务开始：{name}")
            try:
                result = fn(runtime)
                task_rows.append(_build_task_summary_row(name, result))
                persisted_rows.extend(_collect_persisted_rows(name, result))
                risk_bucket = _collect_risk_bucket(name, result)
                task_data = result if isinstance(result, dict) else {}
                task_ok = _to_int(task_data.get("ok"), 0)
                task_skip = _to_int(task_data.get("skip"), 0)
                task_manual = _to_int(task_data.get("manual_review"), 0)
                for source, count in risk_bucket.items():
                    slot = risk_stats.setdefault(source, {"count": 0, "auto_passed": 0, "manual_review": 0, "skipped": 0})
                    slot["count"] += _to_int(count, 0)
                    slot["manual_review"] += min(task_manual, _to_int(count, 0))
                    slot["skipped"] += min(task_skip, _to_int(count, 0))
                    slot["auto_passed"] += min(task_ok, _to_int(count, 0))
            except Exception as exc:
                error_text = str(exc or "unknown error")
                _log(f"任务失败：{name}（{error_text}）")
                task_rows.append(_build_task_summary_row(name, None, error=error_text))

        _log("本轮任务汇总：")
        _log(
            "\n" + _format_table(
                ["Task", "Total", "OK", "Skip", "Fail", "ManualReview", "Persisted", "Note"],
                task_rows,
            )
        )

        if persisted_rows:
            _log("本轮改价记录：")
            _log(
                "\n" + _format_table(
                    ["任务", "账号", "商品", "原价", "新价", "差价", "型号", "成色", "质检码", "IMEI", "到手价", "调价时间", "触发器"],
                    persisted_rows,
                )
            )
        else:
            _log("本轮改价记录：无已落库改价")

        risk_rows = _build_risk_summary_rows(risk_stats)
        if risk_rows:
            _log("本轮风险汇总：")
            _log(
                "\n" + _format_table(
                    ["风险来源", "条数", "自动通过", "待人工确认", "跳过"],
                    risk_rows,
                )
            )
            risk_summary_text = _risk_natural_summary(risk_stats)
            _log(risk_summary_text)
        else:
            risk_summary_text = "本轮无风险标签数据"
            _log("本轮风险汇总：无风险分桶数据")

        inventory_after = _snapshot_inventory_state(runtime)
        inventory_change_rows = _collect_inventory_changes(inventory_before, inventory_after)
        if inventory_change_rows:
            _log("本轮库存状态变化：")
            _log("\n" + _format_table(["Account", "Item", "From", "To"], inventory_change_rows[:20]))
        else:
            _log("本轮库存状态变化：无状态变化")

        turnover_metrics = _collect_turnover_metrics(runtime, target_date=turnover_date)
        sold_today = _to_int(turnover_metrics.get("sold_today"), 0)
        on_sale_count = _to_int(turnover_metrics.get("on_sale_count"), 0)
        turnover_rate = float(turnover_metrics.get("turnover_rate") or 0.0)
        _log(f"动销统计：今日销售 {sold_today}，在架数量 {on_sale_count}，动销率 {turnover_rate:.2%}")

        cycle_report_text = _build_cycle_report_message(
            cycle=cycles,
            task_rows=task_rows,
            persisted_rows=persisted_rows,
            risk_stats=risk_stats,
            risk_summary_text=risk_summary_text,
            inventory_change_rows=inventory_change_rows,
            turnover_metrics=turnover_metrics,
        )
        cycle_payload = {
            "ts": _now_text(),
            "cycle": cycles,
            "tasks": tasks,
            "task_rows": task_rows,
            "persisted_rows": persisted_rows,
            "risk_stats": risk_stats,
            "risk_summary": risk_summary_text,
            "inventory_change_rows": inventory_change_rows,
            "turnover_metrics": turnover_metrics,
            "post_manual_review_mode": post_manual_review_mode,
            "post_manual_review_source_filter": sorted(source_filter),
            "agent_user_id": str(agent_user_id or "").strip() or "anonymous",
            "run_mode": "run",
        }
        logged, log_target = _append_cycle_jsonl(cycle_payload)
        if logged:
            _log(f"本轮日志已写入：{log_target}")
        else:
            _log(f"本轮日志写入失败：{log_target}")

        total_ok = sum(_to_int(row[2], 0) for row in task_rows if len(row) >= 3)
        total_skip = sum(_to_int(row[3], 0) for row in task_rows if len(row) >= 4)
        total_fail = sum(_to_int(row[4], 0) for row in task_rows if len(row) >= 5)
        total_manual = sum(_to_int(row[5], 0) for row in task_rows if len(row) >= 6)
        usage_entry = {
            "agent_user_id": str(agent_user_id or "").strip() or "anonymous",
            "ts": _now_text(),
            "command": "run",
            "tasks": ",".join(tasks),
            "ok": total_ok,
            "skip": total_skip,
            "fail": total_fail,
            "manual_review": total_manual,
            "persisted": len(persisted_rows),
            "turnover_rate": f"{turnover_rate:.2%}",
            "note": risk_summary_text,
        }
        app_id, app_secret, app_token, table_id = _usage_bitable_target(runtime)
        usage_ok, usage_reason = _write_usage_to_bitable(
            app_id=app_id,
            app_secret=app_secret,
            app_token=app_token,
            table_id=table_id,
            entry=usage_entry,
        )
        _log(f"使用记录上报结果：{'已上传' if usage_ok else '未上传'}（{usage_reason}）")
        try:
            get_mysql_store().write_usage_log(usage_entry)
        except Exception:
            pass

        pushed, push_reason = _push_cycle_report(runtime, cycle_report_text)
        if pushed:
            _log(f"轮次汇总已发送（{push_reason}）")
        else:
            _log(f"轮次汇总发送失败（{push_reason}）")

        if bool(cfg.get("mysql_delta_report_enabled", True)):
            mysql_current_counts, mysql_current_errors = _collect_mysql_table_counts()
            mysql_deltas: dict[str, int] = {}
            for table in _mysql_summary_tables():
                base = int(mysql_baseline_counts.get(table, 0))
                curr = int(mysql_current_counts.get(table, base))
                mysql_deltas[table] = curr - base
            mysql_delta_message = _build_mysql_delta_summary_message(
                cycle=cycles,
                deltas=mysql_deltas,
                errors=mysql_current_errors,
            )
            delta_pushed, delta_reason = _push_cycle_report(runtime, mysql_delta_message)
            if delta_pushed:
                _log(f"MySQL 增量摘要已发送（{delta_reason}）")
            else:
                _log(f"MySQL 增量摘要发送失败（{delta_reason}）")
            mysql_baseline_counts = dict(mysql_current_counts)
            mysql_baseline_errors = dict(mysql_current_errors)

        if post_manual_review_mode == "off":
            _log("本轮未开启待确认自动处理（mode=off，保持人工处理）")
        else:
            _log(f"本轮开始同步处理待确认（模式：{post_manual_review_mode}）")
            try:
                post_review_code = _resolve_pending_manual_review(
                    post_manual_review_mode,
                    runtime=runtime,
                    source_filter=source_filter,
                )
                if post_review_code != 0:
                    _log(f"待确认自动处理返回异常状态：exit={post_review_code}")
            except Exception as exc:
                _log(f"待确认同步处理异常：{exc}")

        try:
            runtime.imported_store.save_to_file(runtime.imported_pool_file)
            _log(f"导入商品池已保存: {runtime.imported_store.count()} 条")
        except Exception as exc:
            _log(f"导入商品池保存失败: {exc}")

        should_upload, upload_remaining = _should_run_cloud_sync("upload")
        if should_upload:
            _log("开始上传本地数据到云端")
            try:
                sync_code = int(migrate_local_to_mysql(on_progress=_build_sync_progress_logger("云端上传")))
                _log(f"本轮数据上传完成：exit={sync_code}")
                if sync_code == 0:
                    _mark_cloud_sync_ran("upload")
            except Exception as exc:
                _log(f"本轮数据上传失败：{exc}")
        else:
            _log(f"跳过本轮云端上传：距离下次上传剩余 {upload_remaining}s")

        _log(f"第 {cycles} 轮完成")

        if once:
            return 0
        if cap > 0 and cycles >= cap:
            return 0
        next_time = datetime.datetime.now() + datetime.timedelta(seconds=interval)
        _log(f"第 {cycles} 轮等待中：{interval}s 后继续（预计 {next_time.strftime('%H:%M:%S')}）")
        time.sleep(interval)


def main() -> int:
    args = _parse_args()

    if args.command in (None, "run", "post-qc-report", "posterior-backfill-pool", "feishu-listen", "data-sync", "bitable-sync", "validate-bitable-inventory", "provision-user-inventory", "show-user-inventory-binding"):
        auth_cfg = _load_agent_auth_config()
        user_id = str(getattr(args, "agent_user_id", "") or "").strip()
        access_key = str(getattr(args, "agent_access_key", "") or "")
        if bool(auth_cfg.get("auth_required", False)) and not user_id:
            user_id = input("Agent 用户名: ").strip()
        if bool(auth_cfg.get("auth_required", False)) and not access_key:
            access_key = getpass.getpass("Agent 密码: ").strip()
        setattr(args, "agent_user_id", user_id)
        auth_ok, auth_reason = _verify_agent_access(user_id=user_id, access_key=access_key)
        if not auth_ok:
            _log(f"Agent 鉴权失败：{auth_reason}")
            return 1
        if user_id:
            _log(f"Agent 鉴权通过：用户 {user_id}")

    if args.command in (None, "run"):
        sync_started, sync_code, sync_reason = _maybe_auto_sync_to_mysql(
            force_enable=bool(getattr(args, "auto_sync_mysql", False)),
            force_disable=bool(getattr(args, "no_auto_sync_mysql", False)),
        )
        if sync_started and sync_code == 0:
            _log("已从云端同步数据")
        elif sync_started and sync_code != 0:
            mode = str(cfg.get("mysql_auto_sync_on_run_mode", "best_effort") or "best_effort").strip().lower()
            _log(f"云端同步失败：{sync_reason}")
            if mode == "strict":
                _log("严格模式下同步失败，已停止运行")
                return int(sync_code or 1)
        else:
            _log(f"本轮跳过云端同步：{sync_reason}")

        task_names = _normalize_task_names(getattr(args, "tasks", "erp_sync,auto_reprice"))
        turnover_date = _parse_turnover_date(getattr(args, "turnover_date", ""))
        return run_loop(
            task_names,
            interval_seconds=int(getattr(args, "interval_seconds", 300) or 300),
            once=bool(getattr(args, "once", False)),
            max_cycles=int(getattr(args, "max_cycles", 0) or 0),
            post_manual_review_mode=str(getattr(args, "post_manual_review_mode", "off") or "off"),
            post_manual_review_source_filter=str(getattr(args, "post_manual_review_source_filter", "") or ""),
            agent_user_id=str(getattr(args, "agent_user_id", "") or "").strip(),
            turnover_date=turnover_date,
        )

    if args.command == "post-qc-report":
        runtime = AgentRuntime()
        return _run_post_qc_report(runtime, args)

    if args.command == "posterior-backfill-pool":
        runtime = AgentRuntime()
        return _run_posterior_backfill_pool(runtime, args)

    if args.command == "feishu-listen":
        runtime = AgentRuntime()
        return _run_feishu_event_listener(runtime, args)

    if args.command == "data-sync":
        sync_to = str(getattr(args, "to", "mysql") or "mysql").strip().lower()
        if sync_to != "mysql":
            _log(f"不支持的数据同步目标：{sync_to}")
            return 1
        _log("开始上传本地数据到云端")
        code = int(migrate_local_to_mysql(on_progress=_build_sync_progress_logger("云端上传")))
        _log(f"数据上传完成：exit={code}")
        return code

    if args.command == "validate-bitable-inventory":
        return _validate_bitable_inventory(args)

    if args.command == "provision-user-inventory":
        return _provision_user_inventory_binding(args)

    if args.command == "show-user-inventory-binding":
        return _show_user_inventory_binding(args)

    if args.command == "bitable-sync":
        runtime = AgentRuntime()
        return _run_bitable_inventory_sync(args, runtime=runtime)

    if args.command == "config":
        sub = getattr(args, "config_command", None)
        if sub == "add-account":
            return _save_account(args)
        if sub == "add-account-interactive":
            return _save_account_interactive(args)
        if sub == "open-cookie-manager":
            ok, message = _open_cookie_manager()
            _log(message)
            return 0 if ok else 1
        if sub == "list-accounts":
            return _list_accounts()
        if sub == "validate-cookies":
            summary = _validate_cookies(include_disabled=bool(getattr(args, "all", False)))
            _print_validation_summary(summary)
            return 0 if len(summary.get("failed") or []) == 0 else 2
        if sub == "resolve-manual-review":
            return _resolve_pending_manual_review(str(getattr(args, "mode", "reject")))
        if sub == "manual-review-interactive":
            return _manual_review_interactive(int(getattr(args, "timeout_seconds", 20) or 20))
        _log("未知的 config 子命令")
        return 1

    _log(f"未知命令：{args.command}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
