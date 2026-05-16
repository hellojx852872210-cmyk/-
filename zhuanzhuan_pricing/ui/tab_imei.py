# -*- coding: utf-8 -*-
"""
质检码/IMEI 改价 Tab — 单品操作
"""
from __future__ import annotations
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import ZhuanzhuanContext

from ..core.models import PriceChangeRecord, PriceTrigger
from ..core.pricing_engine import PricingEngine
from ..services.zhuanzhuan_api import ImeiService


class ImeiTab:
    def __init__(self, parent: ttk.Notebook, ctx: "ZhuanzhuanContext"):
        self.ctx = ctx
        self.frame = ttk.Frame(parent)
        parent.add(self.frame, text="📱 质检码改价")
        self._current_detail = None
        self._current_pricing = None
        self._preview_after_id = None
        self._preview_request_token = 0
        self._build_ui()

    def _build_ui(self):
        query_frame = ttk.LabelFrame(self.frame, text="查询商品")
        query_frame.pack(fill="x", padx=8, pady=6)

        ttk.Label(query_frame, text="质检码 / IMEI:").grid(row=0, column=0, padx=4, pady=6)
        self._code_var = tk.StringVar()
        entry = ttk.Entry(query_frame, textvariable=self._code_var, width=24)
        entry.grid(row=0, column=1, padx=4)
        entry.bind("<Return>", lambda _: self._query())

        ttk.Label(query_frame, text="账号:").grid(row=0, column=2, padx=4)
        self._account_var = tk.StringVar()
        self._account_combo = ttk.Combobox(query_frame, textvariable=self._account_var, width=16, state="readonly")
        self._account_combo.grid(row=0, column=3, padx=4)
        self._refresh_accounts()

        ttk.Button(query_frame, text="🔍 查询", command=self._query).grid(row=0, column=4, padx=6)

        info_frame = ttk.LabelFrame(self.frame, text="商品信息")
        info_frame.pack(fill="x", padx=8, pady=4)

        info_fields = [
            ("title", "商品名"), ("model", "型号"), ("condition", "成色"),
            ("capacity", "容量"), ("color", "颜色"), ("status", "状态"),
        ]
        self._info_vars: dict[str, tk.StringVar] = {}
        for col, (key, label) in enumerate(info_fields):
            ttk.Label(info_frame, text=f"{label}:").grid(row=0, column=col * 2, padx=4, pady=6, sticky="e")
            var = tk.StringVar(value="—")
            self._info_vars[key] = var
            ttk.Label(info_frame, textvariable=var, width=14).grid(row=0, column=col * 2 + 1, padx=4)

        price_frame = ttk.LabelFrame(self.frame, text="价格")
        price_frame.pack(fill="x", padx=8, pady=4)

        price_kpis = [
            ("current_price", "当前挂牌价"),
            ("settle_price", "当前到手价"),
            ("preview_settle", "预计到手价"),
            ("fast_price", "极速动销价"),
            ("cons_price", "保守动销价"),
            ("floor_price", "底价预警"),
            ("cost_price", "成本价"),
        ]
        self._price_vars: dict[str, tk.StringVar] = {}
        for col, (key, label) in enumerate(price_kpis):
            f = ttk.Frame(price_frame)
            f.grid(row=0, column=col, padx=14, pady=10)
            ttk.Label(f, text=label, foreground="gray").pack()
            var = tk.StringVar(value="—")
            self._price_vars[key] = var
            ttk.Label(f, textvariable=var, font=("微软雅黑", 14, "bold")).pack()

        action_frame = ttk.LabelFrame(self.frame, text="改价 / 上架")
        action_frame.pack(fill="x", padx=8, pady=4)

        ttk.Label(action_frame, text="目标价格:").grid(row=0, column=0, padx=4, pady=6)
        self._new_price_var = tk.DoubleVar(value=0)
        self._new_price_var.trace_add("write", lambda *_: self._update_preview_settle())
        ttk.Entry(action_frame, textvariable=self._new_price_var, width=12).grid(row=0, column=1, padx=4)

        ttk.Button(action_frame, text="📋 用极速价", command=lambda: self._fill_price("fast")).grid(row=0, column=2, padx=4)
        ttk.Button(action_frame, text="📋 用保守价", command=lambda: self._fill_price("cons")).grid(row=0, column=3, padx=4)
        ttk.Button(action_frame, text="💰 执行改价", command=self._do_change_price).grid(row=0, column=4, padx=8)
        ttk.Button(action_frame, text="📤 定价上架", command=self._do_list).grid(row=0, column=5, padx=4)

        self._action_result = tk.StringVar(value="")
        ttk.Label(action_frame, textvariable=self._action_result).grid(row=1, column=0, columnspan=6, pady=4)

        self._progress = ttk.Progressbar(self.frame, mode="indeterminate")
        self._progress.pack(fill="x", padx=8, pady=2)

        self._update_preview_settle()

    def _refresh_accounts(self):
        names = [a.name for a in self.ctx.account_store.enabled_accounts()]
        self._account_combo["values"] = names
        if names:
            self._account_var.set(names[0])

    def _query(self):
        code = self._code_var.get().strip()
        account_name = self._account_var.get()
        if not code or not account_name:
            messagebox.showwarning("提示", "请填写质检码和选择账号")
            return
        self._progress.start(10)
        self._action_result.set("查询中 …")
        threading.Thread(target=self._do_query, args=(code, account_name), daemon=True).start()

    def _do_query(self, code: str, account_name: str):
        detail, error = self._fetch_detail(code, account_name)
        if error:
            self.frame.after(0, lambda e=error: self._action_result.set(f"查询失败: {e}"))
            self.frame.after(0, self._progress.stop)
            return

        self._current_detail = detail
        self.frame.after(0, lambda: self._fill_detail(detail, account_name))
        self.frame.after(0, self._progress.stop)

    def _fetch_detail(self, code: str, account_name: str):
        accounts = {a.name: a for a in self.ctx.account_store.load_all()}
        account = accounts.get(account_name)
        if not account:
            return None, "账号不存在"
        try:
            svc = ImeiService(account.name, account.cookie)
            if len(code) == 15 and code.isdigit():
                return svc.query_by_imei(code), None
            return svc.query_by_qc_code(code), None
        except Exception as e:
            return None, str(e)

    def _fill_detail(self, detail, account_name: str):
        if not detail:
            self._action_result.set("未找到商品")
            return

        for key in self._info_vars:
            if key == "status":
                self._info_vars[key].set(self._format_status(detail))
                continue
            val = getattr(detail, key, "—")
            if hasattr(val, "value"):
                val = val.value
            self._info_vars[key].set(str(val))

        self._price_vars["current_price"].set(f"¥{detail.current_price:,.0f}")
        if detail.settle_price and detail.settle_price > 0:
            self._price_vars["settle_price"].set(f"¥{detail.settle_price:,.1f}")
        else:
            self._price_vars["settle_price"].set("—")

        cost = self.ctx.cost_map.get(detail.product_id)
        self._price_vars["cost_price"].set(f"¥{cost:,.0f}" if cost else "—")

        records = self.ctx.sold_cache.filter_by_model_key(
            detail.model, detail.condition, detail.capacity, detail.color
        )
        pricing = self.ctx.pricing_engine.calculate(
            records, detail.model, detail.condition, detail.capacity, detail.color
        )
        self._price_vars["fast_price"].set(f"¥{pricing.fast_price:,.0f}" if pricing.fast_price else "—")
        self._price_vars["cons_price"].set(f"¥{pricing.cons_price:,.0f}" if pricing.cons_price else "—")
        self._price_vars["floor_price"].set(f"¥{pricing.floor_price:,.0f}" if pricing.floor_price else "—")
        self._current_pricing = pricing
        self._preview_request_token += 1
        self._update_preview_settle()
        self._action_result.set("查询完成")

    def _format_status(self, detail) -> str:
        status = getattr(detail, "status", None)
        label = getattr(status, "label", "")
        if label and label != "未知":
            return label
        status_text = str(getattr(detail, "status_text", "") or "").strip()
        if status_text and status_text not in {"-1", "UNKNOWN", "unknown"}:
            return status_text
        raw = getattr(status, "value", status)
        raw_text = str(raw or "").strip()
        return f"未知状态({raw_text})" if raw_text else "未知"

    def _fill_price(self, which: str):
        if not self._current_pricing:
            return
        price = self._current_pricing.fast_price if which == "fast" else self._current_pricing.cons_price
        if price:
            self._new_price_var.set(round(price, 0))

    def _update_preview_settle(self):
        try:
            price = float(self._new_price_var.get())
        except Exception:
            price = 0
        if self._preview_after_id:
            try:
                self.frame.after_cancel(self._preview_after_id)
            except Exception:
                pass
            self._preview_after_id = None
        if price <= 0 or not self._current_detail:
            self._price_vars["preview_settle"].set("—")
            return
        self._price_vars["preview_settle"].set("查询中 …")
        self._preview_request_token += 1
        token = self._preview_request_token
        self._preview_after_id = self.frame.after(300, lambda p=price, t=token: self._request_preview_settle(p, t))

    def _request_preview_settle(self, price: float, token: int):
        self._preview_after_id = None
        detail = self._current_detail
        account_name = self._account_var.get()
        if not detail or price <= 0 or not account_name:
            self._price_vars["preview_settle"].set("—")
            return
        threading.Thread(
            target=self._fetch_preview_settle,
            args=(detail.product_id, account_name, price, token),
            daemon=True,
        ).start()

    def _fetch_preview_settle(self, product_id: str, account_name: str, price: float, token: int):
        accounts = {a.name: a for a in self.ctx.account_store.load_all()}
        account = accounts.get(account_name)
        if not account:
            self.frame.after(0, lambda t=token: self._apply_preview_settle(t, None))
            return
        svc = ImeiService(account.name, account.cookie)
        settle = svc.estimate_settle_price(product_id, price)
        self.frame.after(0, lambda t=token, s=settle: self._apply_preview_settle(t, s))

    def _apply_preview_settle(self, token: int, settle: float | None):
        if token != self._preview_request_token:
            return
        self._price_vars["preview_settle"].set(f"¥{settle:,.1f}" if settle is not None else "—")

    def _do_change_price(self):
        if not self._current_detail:
            messagebox.showwarning("提示", "请先查询商品")
            return
        new_price = self._new_price_var.get()
        if new_price <= 0:
            messagebox.showwarning("提示", "价格无效")
            return
        self._progress.start(10)
        threading.Thread(target=self._exec_change_price, args=(self._current_detail, new_price), daemon=True).start()

    def _exec_change_price(self, detail, new_price: float):
        account_name = self._account_var.get()
        accounts = {a.name: a for a in self.ctx.account_store.load_all()}
        account = accounts.get(account_name)
        if not account:
            self.frame.after(0, lambda: self._action_result.set("账号不存在"))
            self.frame.after(0, self._progress.stop)
            return

        svc = ImeiService(account.name, account.cookie)
        ok, msg = svc.change_price(detail, new_price)
        refreshed_detail = detail

        if ok:
            settle = PricingEngine.calc_settle_price(new_price)
            self.ctx.history_db.record(PriceChangeRecord(
                id=None,
                timestamp=datetime.datetime.now(),
                product_id=detail.product_id, qc_code=detail.qc_code,
                title=detail.title, model=detail.model,
                condition=detail.condition, capacity=detail.capacity,
                color=detail.color,
                old_price=detail.current_price, new_price=new_price,
                diff=new_price - detail.current_price,
                settle_price=settle,
                trigger=PriceTrigger.MANUAL,
                account_name=account_name,
            ))
            refreshed_detail = svc.query_by_qc_code(detail.qc_code) or svc.query_by_imei(detail.imei) or detail
            self._current_detail = refreshed_detail

        self.frame.after(0, lambda ok=ok, msg=msg, refreshed_detail=refreshed_detail, account_name=account_name: self._after_change_price(ok, msg, refreshed_detail, account_name))

    def _after_change_price(self, ok: bool, msg: str, detail, account_name: str):
        if ok and detail:
            self._fill_detail(detail, account_name)
        self._action_result.set(f"{'✓ 改价成功' if ok else '✗ 失败'}: {msg}")
        self._progress.stop()

    def _do_list(self):
        if not self._current_detail:
            messagebox.showwarning("提示", "请先查询商品")
            return
        new_price = self._new_price_var.get()
        if new_price <= 0:
            messagebox.showwarning("提示", "请填写上架价格")
            return
        account_name = self._account_var.get()
        accounts = {a.name: a for a in self.ctx.account_store.load_all()}
        account = accounts.get(account_name)
        if not account:
            return
        svc = ImeiService(account.name, account.cookie)
        ok, msg = svc.list_product(self._current_detail.product_id, new_price, self._current_detail.qc_code)
        refreshed_detail = self._current_detail
        if ok:
            refreshed_detail = svc.query_by_qc_code(self._current_detail.qc_code) or svc.query_by_imei(self._current_detail.imei) or self._current_detail
            self._current_detail = refreshed_detail
            self._fill_detail(refreshed_detail, account_name)
        self._action_result.set(f"{'✓ 上架成功' if ok else '✗ 失败'}: {msg}")
