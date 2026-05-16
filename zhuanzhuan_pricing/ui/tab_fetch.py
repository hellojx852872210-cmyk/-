# -*- coding: utf-8 -*-
"""
数据管理 Tab — 账号管理 + 历史成交同步
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import ttk, messagebox
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import ZhuanzhuanContext

from ..core.models import Account
from ..services.zhuanzhuan_api import DataFetcher


class FetchTab:
    SOLD_CACHE_UPDATED_EVENT = "<<ZhuanzhuanSoldCacheUpdated>>"
    IMPORTED_STORE_UPDATED_EVENT = "<<ZhuanzhuanImportedStoreUpdated>>"

    SYNC_RANGE_OPTIONS = {
        "全部": 0,
        "近 7 天": 7,
        "近 14 天": 14,
        "近 30 天": 30,
        "近 90 天": 90,
    }
    BROWSE_RANGE_OPTIONS = {
        "全部": 0,
        "近 7 天": 7,
        "近 14 天": 14,
        "近 30 天": 30,
        "近 90 天": 90,
    }

    def __init__(self, parent: ttk.Notebook, ctx: "ZhuanzhuanContext"):
        self.ctx = ctx
        self.frame = ttk.Frame(parent)
        self._event_target = parent
        self._account_test_in_progress = False
        self._account_test_result = None
        self._browse_boxes: dict[str, ttk.Combobox] = {}
        parent.add(self.frame, text="⚙️ 数据管理")
        self._build_ui()
        self._refresh_account_list()
        self._refresh_browser_options()
        self._refresh_browser_view()

    def _emit_sold_cache_updated(self):
        if self._event_target is None:
            return
        for event_name in (self.SOLD_CACHE_UPDATED_EVENT, "<<SoldCacheUpdated>>"):
            try:
                self._event_target.event_generate(event_name)
            except Exception:
                pass

    def _emit_imported_store_updated(self):
        if self._event_target is None:
            return
        for event_name in (self.IMPORTED_STORE_UPDATED_EVENT, "<<ImportedStoreUpdated>>"):
            try:
                self._event_target.event_generate(event_name)
            except Exception:
                pass

    def _build_ui(self):
        root = ttk.Frame(self.frame)
        root.pack(fill="both", expand=True)

        canvas = tk.Canvas(root, highlightthickness=0)
        vbar = ttk.Scrollbar(root, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        content = ttk.Frame(canvas)
        content_window = canvas.create_window((0, 0), window=content, anchor="nw")

        def _update_scrollregion(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _fit_content_width(event):
            canvas.itemconfigure(content_window, width=event.width)

        content.bind("<Configure>", _update_scrollregion)
        canvas.bind("<Configure>", _fit_content_width)

        acct_frame = ttk.LabelFrame(content, text="账号管理")
        acct_frame.pack(fill="x", padx=8, pady=6)

        col_frame = ttk.Frame(acct_frame)
        col_frame.pack(fill="x", padx=4, pady=4)

        self._acct_list = tk.Listbox(col_frame, height=6, width=20)
        self._acct_list.pack(side="left", fill="y")
        self._acct_list.bind("<<ListboxSelect>>", self._on_acct_select)

        form = ttk.Frame(col_frame)
        form.pack(side="left", fill="both", expand=True, padx=8)

        ttk.Label(form, text="账号名称:").grid(row=0, column=0, sticky="w", pady=3)
        self._acct_name = tk.StringVar()
        ttk.Entry(form, textvariable=self._acct_name, width=24).grid(row=0, column=1, sticky="ew")

        ttk.Label(form, text="Cookie:").grid(row=1, column=0, sticky="nw", pady=3)
        self._acct_cookie = tk.Text(form, width=50, height=4, wrap="word", font=("Consolas", 9))
        self._acct_cookie.grid(row=1, column=1, sticky="ew")

        ttk.Label(form, text="备注:").grid(row=2, column=0, sticky="w", pady=3)
        self._acct_note = tk.StringVar()
        ttk.Entry(form, textvariable=self._acct_note, width=24).grid(row=2, column=1, sticky="ew")

        self._acct_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text="启用此账号", variable=self._acct_enabled).grid(row=3, column=1, sticky="w")

        form.columnconfigure(1, weight=1)

        btn_row = ttk.Frame(acct_frame)
        btn_row.pack(fill="x", padx=4, pady=4)
        ttk.Button(btn_row, text="➕ 保存/更新", command=self._save_account).pack(side="left", padx=2)
        ttk.Button(btn_row, text="🗑️ 删除", command=self._delete_account).pack(side="left", padx=2)
        self._test_btn = ttk.Button(btn_row, text="🔄 测试连接", command=self._test_account)
        self._test_btn.pack(side="left", padx=2)

        sync_frame = ttk.LabelFrame(self.frame, text="历史成交数据同步")
        sync_frame.pack(fill="both", expand=True, padx=8, pady=6)

        sync_bar = ttk.Frame(sync_frame)
        sync_bar.pack(fill="x", padx=4, pady=4)

        ttk.Label(sync_bar, text="同步范围:").pack(side="left")
        self._sync_range_var = tk.StringVar(value="全部")
        ttk.Combobox(
            sync_bar,
            textvariable=self._sync_range_var,
            values=list(self.SYNC_RANGE_OPTIONS.keys()),
            state="readonly",
            width=10,
        ).pack(side="left", padx=(4, 8))

        ttk.Button(sync_bar, text="📥 同步账号成交", command=self._sync_all).pack(side="left", padx=2)
        ttk.Button(sync_bar, text="📥 ERP 导入商品", command=self._sync_erp).pack(side="left", padx=2)
        ttk.Button(sync_bar, text="🗑️ 删除成交缓存", command=self._clear_sold_cache).pack(side="left", padx=2)

        self._cache_label = tk.StringVar(value="")
        ttk.Label(sync_bar, textvariable=self._cache_label, foreground="gray").pack(side="right")

        self._sync_progress = ttk.Progressbar(sync_frame, mode="indeterminate")
        self._sync_progress.pack(fill="x", padx=4, pady=2)

        browser_frame = ttk.LabelFrame(sync_frame, text="成交缓存浏览")
        browser_frame.pack(fill="both", expand=True, padx=4, pady=4)

        filter_frame = ttk.Frame(browser_frame)
        filter_frame.pack(fill="x", padx=4, pady=4)

        self._browse_vars = {
            "model": tk.StringVar(),
            "condition": tk.StringVar(),
            "capacity": tk.StringVar(),
            "color": tk.StringVar(),
            "days": tk.StringVar(value="全部"),
            "keyword": tk.StringVar(),
        }

        filter_specs = [
            ("model", "型号", 22),
            ("condition", "成色", 14),
            ("capacity", "容量", 12),
            ("color", "颜色", 12),
        ]
        for col, (key, label, width) in enumerate(filter_specs):
            ttk.Label(filter_frame, text=f"{label}:").grid(row=0, column=col * 2, padx=4, pady=4, sticky="w")
            box = ttk.Combobox(filter_frame, textvariable=self._browse_vars[key], width=width)
            box.grid(row=0, column=col * 2 + 1, padx=4, pady=4, sticky="ew")
            self._browse_boxes[key] = box

        ttk.Label(filter_frame, text="近 N 天:").grid(row=1, column=0, padx=4, pady=4, sticky="w")
        ttk.Combobox(
            filter_frame,
            textvariable=self._browse_vars["days"],
            values=list(self.BROWSE_RANGE_OPTIONS.keys()),
            state="readonly",
            width=12,
        ).grid(row=1, column=1, padx=4, pady=4, sticky="w")

        ttk.Label(filter_frame, text="关键字:").grid(row=1, column=2, padx=4, pady=4, sticky="w")
        ttk.Entry(filter_frame, textvariable=self._browse_vars["keyword"], width=24).grid(
            row=1, column=3, padx=4, pady=4, sticky="ew"
        )

        ttk.Button(filter_frame, text="🔍 刷新视图", command=self._refresh_browser_view).grid(row=1, column=6, padx=4)
        ttk.Button(filter_frame, text="🧹 清空筛选", command=self._reset_browser_filters).grid(row=1, column=7, padx=4)

        tree_frame = ttk.Frame(browser_frame)
        tree_frame.pack(fill="both", expand=True, padx=4, pady=4)

        cols = [
            ("sold_time", "成交时间", 130),
            ("model", "型号", 150),
            ("condition", "成色", 100),
            ("capacity", "容量", 80),
            ("color", "颜色", 90),
            ("sold_price", "成交价", 80),
            ("hours_to_sell", "动销时长(h)", 90),
            ("source", "来源", 70),
            ("title", "标题", 260),
        ]
        vsb = ttk.Scrollbar(tree_frame, orient="vertical")
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal")
        self._browser_tree = ttk.Treeview(
            tree_frame,
            columns=[c[0] for c in cols],
            show="headings",
            yscrollcommand=vsb.set,
            xscrollcommand=hsb.set,
            height=10,
        )
        vsb.config(command=self._browser_tree.yview)
        hsb.config(command=self._browser_tree.xview)
        for col_id, heading, width in cols:
            self._browser_tree.heading(col_id, text=heading)
            self._browser_tree.column(col_id, width=width, minwidth=50)
        self._browser_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        self._browser_status = tk.StringVar(value="缓存浏览就绪")
        ttk.Label(browser_frame, textvariable=self._browser_status, foreground="gray").pack(anchor="e", padx=4, pady=(0, 2))

        self._sync_log = tk.Text(sync_frame, height=6, state="disabled", font=("Consolas", 9))
        self._sync_log.pack(fill="x", padx=4, pady=2)

        self._update_cache_label()

    def _refresh_account_list(self):
        self._acct_list.delete(0, "end")
        for a in self.ctx.account_store.load_all():
            prefix = "✅" if a.enabled else "⬜"
            self._acct_list.insert("end", f"{prefix} {a.name}")

    def _on_acct_select(self, _event=None):
        sel = self._acct_list.curselection()
        if not sel:
            return
        accounts = self.ctx.account_store.load_all()
        if sel[0] >= len(accounts):
            return
        a = accounts[sel[0]]
        self._acct_name.set(a.name)
        self._acct_cookie.delete("1.0", "end")
        self._acct_cookie.insert("1.0", a.cookie)
        self._acct_note.set(a.note)
        self._acct_enabled.set(a.enabled)

    def _save_account(self):
        name = self._acct_name.get().strip()
        cookie = self._acct_cookie.get("1.0", "end").strip()
        if not name or not cookie:
            messagebox.showwarning("提示", "账号名和 Cookie 不能为空")
            return
        accounts = self.ctx.account_store.load_all()
        existing = {a.name: i for i, a in enumerate(accounts)}
        new_acct = Account(name=name, cookie=cookie, enabled=self._acct_enabled.get(), note=self._acct_note.get())
        if name in existing:
            accounts[existing[name]] = new_acct
        else:
            accounts.append(new_acct)
        self.ctx.account_store.save_all(accounts)
        self._refresh_account_list()
        messagebox.showinfo("保存", f"账号「{name}」已保存")

    def _delete_account(self):
        sel = self._acct_list.curselection()
        if not sel:
            return
        accounts = self.ctx.account_store.load_all()
        name = accounts[sel[0]].name
        if messagebox.askyesno("删除", f"确认删除账号「{name}」？"):
            del accounts[sel[0]]
            self.ctx.account_store.save_all(accounts)
            self._refresh_account_list()

    def _test_account(self):
        name = self._acct_name.get().strip()
        cookie = self._acct_cookie.get("1.0", "end").strip()
        if not name or not cookie:
            messagebox.showwarning("提示", "请先填写账号信息")
            return
        if self._account_test_in_progress:
            return
        self._account_test_in_progress = True
        self._account_test_result = None
        self._test_btn.config(state="disabled")
        self._log("测试连接中 …")

        def _test():
            from ..services.zhuanzhuan_api import ImeiService
            try:
                svc = ImeiService(name, cookie)
                ok, msg = svc.check_cookie_valid()
                if ok:
                    self._account_test_result = (True, "连接成功")
                else:
                    self._account_test_result = (False, f"连接失败: {msg}")
            except Exception as e:
                self._account_test_result = (False, f"连接失败: {e}")

        threading.Thread(target=_test, daemon=True).start()
        self.frame.after(200, self._poll_account_test_result)

    def _sync_all(self):
        accounts = self.ctx.account_store.enabled_accounts()
        if not accounts:
            messagebox.showwarning("提示", "无可用账号")
            return
        self._sync_progress.start(10)
        days = self.SYNC_RANGE_OPTIONS.get(self._sync_range_var.get(), 0)
        since = datetime.now() - timedelta(days=days) if days else None
        threading.Thread(target=self._do_sync_all, args=(accounts, since), daemon=True).start()

    def _poll_account_test_result(self):
        result = self._account_test_result
        if result is None:
            self.frame.after(200, self._poll_account_test_result)
            return
        self._account_test_in_progress = False
        self._account_test_result = None
        self._test_btn.config(state="normal")
        self._finish_account_test(*result)

    def _finish_account_test(self, ok: bool, message: str):
        self._log(("✓ " if ok else "✗ ") + message)
        if ok:
            messagebox.showinfo("测试连接", message)
        else:
            messagebox.showerror("测试连接失败", message)

    def _do_sync_all(self, accounts, since=None):
        total_added = 0
        total_updated = 0
        range_label = self._sync_range_var.get()
        for account in accounts:
            self.frame.after(0, lambda n=account.name, r=range_label: self._log(f"[{n}] 拉取成交数据（{r}）…"))
            try:
                fetcher = DataFetcher(account.name, account.cookie)
                records = fetcher.fetch_all_sold(since=since)
                added, updated = self.ctx.sold_cache.upsert(records)
                total_added += added
                total_updated += updated
                self.frame.after(0, lambda n=account.name, a=added, u=updated: self._log(f"[{n}] 新增 {a} 条，更新 {u} 条"))
            except Exception as e:
                self.frame.after(0, lambda n=account.name, e=e: self._log(f"[{n}] 失败: {e}"))

        self.frame.after(0, lambda: self._after_sync_done(total_added, total_updated))

    def _after_sync_done(self, total_added: int, total_updated: int = 0):
        self._sync_progress.stop()
        summary = f"同步完成，共新增 {total_added} 条"
        if total_updated:
            summary += f"，更新 {total_updated} 条"
        self._log(summary)
        self._update_cache_label()
        self._refresh_browser_options()
        self._refresh_browser_view()
        self._emit_sold_cache_updated()

    def _clear_sold_cache(self):
        count = self.ctx.sold_cache.count()
        if count <= 0:
            messagebox.showinfo("删除成交缓存", "当前没有可删除的成交缓存")
            return
        if not messagebox.askyesno("删除成交缓存", f"确认删除本地 {count} 条成交缓存？删除后可重新同步。"):
            return
        self.ctx.sold_cache.clear()
        self._log(f"已删除本地成交缓存 {count} 条")
        self._update_cache_label()
        self._refresh_browser_options()
        self._refresh_browser_view()
        self._emit_sold_cache_updated()

    def _sync_erp(self):
        self._sync_progress.start(10)
        threading.Thread(target=self._do_sync_erp, daemon=True).start()

    def _do_sync_erp(self):
        from ..automation.tasks import task_erp_sync
        result = task_erp_sync(
            self.ctx.erp_config,
            self.ctx.cost_map,
            self.ctx.account_store,
            imported_store=self.ctx.imported_store,
            on_progress=lambda msg: self.frame.after(0, lambda m=msg: self._log(m)),
            sold_cache=self.ctx.sold_cache,
            sold_sync_days=30,
        )

        def _finish(summary: str, emit_imported_event: bool = False):
            self._sync_progress.stop()
            self._log(summary)
            if emit_imported_event:
                self._emit_imported_store_updated()

        if isinstance(result, dict):
            summary = result.get("summary", str(result))
            matched = int(result.get("matched", 0) or 0)
            if matched > 0:
                self.frame.after(0, lambda s=summary, m=matched: _finish(f"✅ ERP 导入完成: {s}；商品管理列表现有 {self.ctx.imported_store.count()} 件", emit_imported_event=True))
            else:
                self.frame.after(0, lambda s=summary: _finish(f"ERP 导入完成: {s}", emit_imported_event=True))
        else:
            self.frame.after(0, lambda r=str(result): _finish(f"ERP 导入完成: {r}", emit_imported_event=True))

    def _refresh_browser_options(self):
        options = self.ctx.sold_cache.get_filter_options(fuzzy_model=True)
        for key, box in self._browse_boxes.items():
            box["values"] = [""] + options.get(key, [])

    def _reset_browser_filters(self):
        for key in ("model", "condition", "capacity", "color", "keyword"):
            self._browse_vars[key].set("")
        self._browse_vars["days"].set("全部")
        self._refresh_browser_view()

    def _refresh_browser_view(self):
        days = self.BROWSE_RANGE_OPTIONS.get(self._browse_vars["days"].get(), 0)
        records = self.ctx.sold_cache.query_records(
            model=self._browse_vars["model"].get().strip(),
            condition=self._browse_vars["condition"].get().strip(),
            capacity=self._browse_vars["capacity"].get().strip(),
            color=self._browse_vars["color"].get().strip(),
            days=days,
            keyword=self._browse_vars["keyword"].get().strip(),
            limit=1000,
            fuzzy_model=True,
        )
        for row in self._browser_tree.get_children():
            self._browser_tree.delete(row)
        for r in records:
            self._browser_tree.insert("", "end", values=(
                r.sold_time.strftime("%m-%d %H:%M"),
                r.model[:20],
                r.condition[:14],
                r.capacity[:10],
                r.color[:10],
                f"¥{r.sold_price:,.0f}",
                f"{r.hours_to_sell:.1f}" if r.hours_to_sell is not None else "—",
                r.source,
                r.title[:60],
            ))
        self._browser_status.set(f"当前显示 {len(records)} 条记录")

    def _log(self, msg: str):
        self._sync_log.config(state="normal")
        import datetime as _dt
        self._sync_log.insert("end", f"[{_dt.datetime.now().strftime('%H:%M:%S')}] {msg}\n")
        self._sync_log.see("end")
        self._sync_log.config(state="disabled")

    def _update_cache_label(self):
        count = self.ctx.sold_cache.count()
        self._cache_label.set(f"本地缓存：{count} 条成交记录")
