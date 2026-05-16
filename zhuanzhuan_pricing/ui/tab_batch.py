# -*- coding: utf-8 -*-
"""
批量调价 Tab
"""
from __future__ import annotations
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import datetime
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from .app import ZhuanzhuanContext

from ..automation.tasks import _pricing_preview, build_reprice_decision
from ..core.models import BatchItem, PriceChangeRecord, PriceTrigger
from ..core.pricing_engine import PricingEngine
from ..services.zhuanzhuan_api import ImeiService


class BatchTab:
    """批量调价工作台"""

    PAGE_SIZE = 50

    def __init__(self, parent: ttk.Notebook, ctx: "ZhuanzhuanContext"):
        self.ctx = ctx
        self.frame = ttk.Frame(parent)
        parent.add(self.frame, text="🚀 批量调价")
        self._current_page = 1
        self._filter_text = ""
        self._build_ui()

    # ── UI 构建 ──────────────────────────────────────────────

    def _build_ui(self):
        # 工具栏
        bar = ttk.Frame(self.frame)
        bar.pack(fill="x", padx=8, pady=4)

        ttk.Button(bar, text="📥 拉取在售商品", command=self._fetch_products).pack(side="left", padx=2)
        ttk.Button(bar, text="🔍 匹配建议价",   command=self._match_prices).pack(side="left", padx=2)
        ttk.Button(bar, text="💰 批量改价",      command=self._batch_reprice).pack(side="left", padx=2)
        ttk.Button(bar, text="🗑️ 清空列表",      command=self._clear_list).pack(side="left", padx=2)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)

        ttk.Label(bar, text="筛选:").pack(side="left")
        self._filter_var = tk.StringVar()
        self._filter_var.trace_add("write", lambda *_: self._render_page())
        ttk.Entry(bar, textvariable=self._filter_var, width=18).pack(side="left", padx=2)

        self._status_var = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self._status_var, foreground="gray").pack(side="right")

        # Treeview
        cols = [
            ("qc_code",      "质检码",    90),
            ("title",        "商品名",   180),
            ("condition",    "成色",      60),
            ("capacity",     "容量",      60),
            ("current_price","当前价",    70),
            ("suggest_price","建议价",    70),
            ("diff",         "差价",      60),
            ("settle",       "预计到手价",  84),
            ("cost_price",   "成本价",    70),
            ("stale_days",   "在架天",    60),
            ("status_label", "状态",      70),
            ("account_name", "账号",      80),
        ]
        tree_frame = ttk.Frame(self.frame)
        tree_frame.pack(fill="both", expand=True, padx=8)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical")
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal")
        self._tree = ttk.Treeview(
            tree_frame,
            columns=[c[0] for c in cols],
            show="headings",
            yscrollcommand=vsb.set,
            xscrollcommand=hsb.set,
            selectmode="extended",
        )
        vsb.config(command=self._tree.yview)
        hsb.config(command=self._tree.xview)

        for col_id, heading, width in cols:
            self._tree.heading(col_id, text=heading,
                               command=lambda c=col_id: self._sort_by(c))
            self._tree.column(col_id, width=width, minwidth=40)

        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        # 颜色标签
        self._tree.tag_configure("ok",      background="#f2f8f2")
        self._tree.tag_configure("warn",    background="#fff8e8")
        self._tree.tag_configure("fail",    background="#fff1f1")
        self._tree.tag_configure("stale",   background="#f9f2f7")

        # 分页控制
        pg_bar = ttk.Frame(self.frame)
        pg_bar.pack(fill="x", padx=8, pady=3)
        ttk.Button(pg_bar, text="◀", width=3, command=self._prev_page).pack(side="left")
        self._page_label = ttk.Label(pg_bar, text="第 1 页")
        self._page_label.pack(side="left", padx=6)
        ttk.Button(pg_bar, text="▶", width=3, command=self._next_page).pack(side="left")
        self._total_label = ttk.Label(pg_bar, text="共 0 条", foreground="gray")
        self._total_label.pack(side="left", padx=12)

        # 进度条
        self._progress = ttk.Progressbar(self.frame, mode="indeterminate")
        self._progress.pack(fill="x", padx=8, pady=2)

    # ── 数据加载 ─────────────────────────────────────────────

    def _fetch_products(self):
        accounts = self.ctx.account_store.enabled_accounts()
        if not accounts:
            messagebox.showwarning("提示", "请先在「数据管理」添加账号")
            return

        self._set_busy(True, "正在拉取在售商品 …")
        threading.Thread(target=self._do_fetch, args=(accounts,), daemon=True).start()

    def _do_fetch(self, accounts):
        all_items: List[BatchItem] = []
        for account in accounts:
            try:
                svc = ImeiService(account.name, account.cookie)
                products = svc.fetch_all_on_sale()
                for p in products:
                    item = BatchItem(
                        product_id=p.product_id, qc_code=p.qc_code, title=p.title,
                        model=p.model, condition=p.condition,
                        capacity=p.capacity, color=p.color,
                        current_price=p.current_price, status=p.status,
                        account_name=account.name,
                        listed_time=p.listed_time,
                    )
                    item.cost_price = self.ctx.cost_map.get(p.product_id)
                    all_items.append(item)
            except Exception as e:
                self.frame.after(0, lambda e=e: self._status_var.set(f"拉取失败: {e}"))

        self.ctx.batch_store.set_all(all_items)
        self.frame.after(0, lambda: self._after_fetch(len(all_items)))

    def _after_fetch(self, count: int):
        self._current_page = 1
        self._render_page()
        self._set_busy(False, f"已加载 {count} 件商品")

    # ── 匹配建议价 ────────────────────────────────────────────

    def _match_prices(self):
        items = self.ctx.batch_store.get_all()
        if not items:
            messagebox.showinfo("提示", "请先拉取商品列表")
            return
        self._set_busy(True, "正在匹配建议价 …")
        threading.Thread(target=self._do_match, args=(items,), daemon=True).start()

    def _do_match(self, items: List[BatchItem]):
        total = len(items)
        for i, item in enumerate(items):
            records = self.ctx.sold_cache.filter_by_model_key(
                item.model, item.condition, item.capacity, item.color
            )
            pricing = self.ctx.pricing_engine.calculate(records, item.model, item.condition,
                                                        item.capacity, item.color)
            item.pricing = pricing
            decision = build_reprice_decision(item, pricing, self.ctx.rule_engine, apply_rules=True)
            item.floor_price = pricing.floor_price
            item.settle_price = pricing.settle_price
            item.suggest_price = decision.get("final_price")
            item.suggested_settle_price = decision.get("final_settle_price")
            item.rule_hit = decision.get("rule_hit", "")
            preview = _pricing_preview(pricing, self.ctx.rule_engine, item, decision)
            item.op_status = "已匹配" if item.suggest_price is not None else "跳过"
            item.op_message = preview if item.suggest_price is not None else f"无定价依据｜{preview}"

            if (i + 1) % 10 == 0:
                self.frame.after(0, lambda i=i: self._status_var.set(
                    f"匹配中 {i+1}/{total} …"
                ))

        self.ctx.batch_store.set_all(items)
        self.frame.after(0, lambda: (self._render_page(), self._set_busy(False, "匹配完成")))

    # ── 批量改价 ──────────────────────────────────────────────

    def _batch_reprice(self):
        items = self.ctx.batch_store.get_all()
        targets = [i for i in items if i.suggest_price and
                   abs(i.suggest_price - i.current_price) >= 1]
        if not targets:
            messagebox.showinfo("提示", "没有需要改价的商品（差价<1元的跳过）")
            return

        if not messagebox.askyesno("确认", f"即将对 {len(targets)} 件商品改价，确认？"):
            return

        self._set_busy(True, f"正在改价 0/{len(targets)} …")
        threading.Thread(target=self._do_reprice, args=(targets,), daemon=True).start()

    def _do_reprice(self, targets: List[BatchItem]):
        ok = fail = 0
        # 按账号分组，复用 Session
        from collections import defaultdict
        by_account: dict = defaultdict(list)
        for item in targets:
            by_account[item.account_name].append(item)

        account_svcs = {}
        for account in self.ctx.account_store.enabled_accounts():
            account_svcs[account.name] = ImeiService(account.name, account.cookie)

        history_records = []
        for account_name, items in by_account.items():
            svc = account_svcs.get(account_name)
            if not svc:
                continue
            for item in items:
                new_price = round(item.suggest_price, 0)
                success, msg = svc.change_price(item.product_id, new_price)
                item.new_price = new_price
                item.reprice_ok = success
                item.reprice_msg = msg
                if success:
                    ok += 1
                    settle = PricingEngine.calc_settle_price(new_price)
                    history_records.append(PriceChangeRecord(
                        id=None,
                        timestamp=datetime.datetime.now(),
                        product_id=item.product_id, qc_code=item.qc_code,
                        title=item.title, model=item.model,
                        condition=item.condition, capacity=item.capacity,
                        color=item.color,
                        old_price=item.current_price, new_price=new_price,
                        diff=new_price - item.current_price,
                        settle_price=settle,
                        trigger=PriceTrigger.MANUAL,
                        account_name=account_name,
                        rule_hit=item.rule_hit,
                    ))
                else:
                    fail += 1

                self.frame.after(0, lambda o=ok, f=fail: self._status_var.set(
                    f"改价中 成功{o} 失败{f} …"
                ))

        if history_records:
            self.ctx.history_db.record_many(history_records)

        self.ctx.batch_store.set_all(
            self.ctx.batch_store.get_all()
        )
        self.frame.after(0, lambda: (
            self._render_page(),
            self._set_busy(False, f"改价完成 ✓{ok} ✗{fail}"),
        ))

    # ── 渲染（分页，增量更新）────────────────────────────────

    def _render_page(self):
        items = self.ctx.batch_store.get_all()
        ft = self._filter_var.get().lower()
        if ft:
            items = [i for i in items if ft in i.title.lower()
                     or ft in i.qc_code.lower()
                     or ft in i.model.lower()]

        total = len(items)
        total_pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self._current_page = min(self._current_page, total_pages)
        start = (self._current_page - 1) * self.PAGE_SIZE
        page_items = items[start: start + self.PAGE_SIZE]

        # 增量更新：只清空当前页条目，不重建整个 Treeview
        for row in self._tree.get_children():
            self._tree.delete(row)

        for item in page_items:
            diff = ""
            tags = ()
            if item.suggest_price is not None:
                d = item.suggest_price - item.current_price
                diff = f"{d:+.0f}"
                if item.reprice_ok is True:
                    tags = ("ok",)
                elif item.reprice_ok is False:
                    tags = ("fail",)
                elif item.stale_days and item.stale_days >= 7:
                    tags = ("stale",)
                elif abs(d) > 100:
                    tags = ("warn",)

            settle = ""
            if item.suggest_price:
                settle = f"¥{PricingEngine.calc_settle_price(item.suggest_price):,.0f}"

            self._tree.insert("", "end", values=(
                item.qc_code,
                item.title[:28],
                item.condition,
                item.capacity,
                f"¥{item.current_price:,.0f}",
                f"¥{item.suggest_price:,.0f}" if item.suggest_price else "—",
                diff,
                settle,
                f"¥{item.cost_price:,.0f}" if item.cost_price else "—",
                f"{item.stale_days:.0f}天" if item.stale_days else "—",
                item.reprice_msg or ("✓" if item.reprice_ok else ("✗" if item.reprice_ok is False else "待改价")),
                item.account_name,
            ), tags=tags)

        self._page_label.config(text=f"第 {self._current_page}/{total_pages} 页")
        self._total_label.config(text=f"共 {total} 条")

    def _sort_by(self, col: str):
        # 简单实现：按列名切换排序（可扩展）
        pass

    # ── 分页 ─────────────────────────────────────────────────

    def _prev_page(self):
        if self._current_page > 1:
            self._current_page -= 1
            self._render_page()

    def _next_page(self):
        items = self.ctx.batch_store.get_all()
        total_pages = max(1, (len(items) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        if self._current_page < total_pages:
            self._current_page += 1
            self._render_page()

    def _clear_list(self):
        self.ctx.batch_store.set_all([])
        self._render_page()
        self._status_var.set("已清空")

    # ── 工具 ─────────────────────────────────────────────────

    def _set_busy(self, busy: bool, status: str = ""):
        if busy:
            self._progress.start(10)
        else:
            self._progress.stop()
        if status:
            self._status_var.set(status)
