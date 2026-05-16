# -*- coding: utf-8 -*-
"""ERP 导入商品管理窗口"""
from __future__ import annotations

import datetime
import re
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..app import ZhuanzhuanContext

from ...automation.tasks import _pricing_preview, _refresh_imported_detail
from ...services.reprice_service import run_reprice_pipeline
from ...core.models import BatchItem, PriceChangeRecord, PriceTrigger, ProductStatus
from ...core.pricing_engine import PricingEngine
from ...services.zhuanzhuan_api import ImeiService


class ImportedProductsWindow(tk.Toplevel):
    PAGE_SIZE = 200
    IMPORT_WORKERS = 6

    IMPORTED_STORE_UPDATED_EVENT = "<<ZhuanzhuanImportedStoreUpdated>>"

    def __init__(self, parent, ctx: "ZhuanzhuanContext", event_target=None, on_close=None):
        super().__init__(parent)
        self.ctx = ctx
        self._event_target = event_target
        self._on_close_callback = on_close
        self._busy = False
        self.title("🗂️ 商品管理（ERP 导入）")
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        width = min(1380, max(960, screen_w - 80))
        height = min(760, max(560, screen_h - 120))
        x = max((screen_w - width) // 2, 0)
        y = max((screen_h - height) // 2, 0)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(960, 560)
        self._filter_var = tk.StringVar()
        self._status_filter_var = tk.StringVar(value="全部")
        self._summary_var = tk.StringVar(value="已导入 0 件商品")
        self._status_var = tk.StringVar(value="就绪")
        self._selected_var = tk.StringVar(value="已勾选 0/0（总 0）")
        self._import_account_var = tk.StringVar()
        self._condition_var = tk.StringVar()
        self._condition_hint_var = tk.StringVar(value="先在表格里点中 1 件商品，再调整成色。")
        self._build_ui()
        self._filter_var.trace_add("write", lambda *_: self._render())
        self._status_filter_var.trace_add("write", lambda *_: self._render())
        self.protocol("WM_DELETE_WINDOW", self._handle_close)
        self._refresh_account_options()
        self._render()

    def _build_ui(self):
        import_frame = ttk.LabelFrame(self, text="手动批量导入 IMEI / 质检码")
        import_frame.pack(fill="x", padx=8, pady=(6, 2))

        import_top = ttk.Frame(import_frame)
        import_top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(import_top, text="操作店铺:").pack(side="left")
        self._account_combo = ttk.Combobox(
            import_top,
            textvariable=self._import_account_var,
            state="readonly",
            width=22,
        )
        self._account_combo.pack(side="left", padx=(4, 8))
        ttk.Button(import_top, text="🔄 刷新店铺", command=self._refresh_account_options).pack(side="left", padx=2)
        ttk.Button(import_top, text="📥 查询并导入", command=self._import_codes).pack(side="left", padx=2)
        ttk.Label(import_top, text="支持换行、逗号、空格批量粘贴", foreground="gray").pack(side="left", padx=10)

        self._import_text = tk.Text(import_frame, height=5, wrap="word")
        self._import_text.pack(fill="x", padx=8, pady=(0, 8))

        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=8, pady=6)

        ttk.Button(toolbar, text="🔄 刷新列表", command=self._render).pack(side="left", padx=2)
        ttk.Button(toolbar, text="☑ 全选当前", command=self._check_all_visible).pack(side="left", padx=2)
        ttk.Button(toolbar, text="☐ 取消勾选", command=self._uncheck_all_visible).pack(side="left", padx=2)
        ttk.Button(toolbar, text="🔁 反选当前", command=self._invert_visible_checked).pack(side="left", padx=2)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(toolbar, text="🔍 刷新勾选商品", command=self._refresh_selected).pack(side="left", padx=2)
        ttk.Button(toolbar, text="📊 匹配建议价", command=self._match_selected).pack(side="left", padx=2)
        ttk.Button(toolbar, text="👁️ 当前行预览", command=self._match_current_item).pack(side="left", padx=2)
        ttk.Button(toolbar, text="💰 执行改价/上架", command=self._apply_selected).pack(side="left", padx=2)
        ttk.Button(toolbar, text="🗑️ 移除勾选", command=self._remove_selected).pack(side="left", padx=2)
        ttk.Button(toolbar, text="🧹 清空导入", command=self._clear_all).pack(side="left", padx=2)

        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(toolbar, text="状态:").pack(side="left")
        self._status_filter_combo = ttk.Combobox(
            toolbar,
            textvariable=self._status_filter_var,
            state="readonly",
            width=10,
            values=("全部", "在售", "未上架", "已售", "已下架", "质检中", "未知"),
        )
        self._status_filter_combo.pack(side="left", padx=(4, 8))

        ttk.Label(toolbar, text="筛选:").pack(side="left")
        ttk.Entry(toolbar, textvariable=self._filter_var, width=22).pack(side="left", padx=4)

        ttk.Label(toolbar, textvariable=self._selected_var, foreground="gray").pack(side="right", padx=(0, 8))
        ttk.Label(toolbar, textvariable=self._summary_var, foreground="gray").pack(side="right")

        condition_frame = ttk.LabelFrame(self, text="成色修正")
        condition_frame.pack(fill="x", padx=8, pady=(0, 4))

        ttk.Label(condition_frame, text="成色:").pack(side="left", padx=(8, 4), pady=6)
        self._condition_combo = ttk.Combobox(
            condition_frame,
            textvariable=self._condition_var,
            state="disabled",
            width=18,
        )
        self._condition_combo.pack(side="left", padx=(0, 6), pady=6)
        self._apply_condition_current_btn = ttk.Button(
            condition_frame,
            text="应用到当前预览商品",
            command=self._apply_condition_to_current,
            state="disabled",
        )
        self._apply_condition_current_btn.pack(side="left", padx=2, pady=6)
        self._apply_condition_checked_btn = ttk.Button(
            condition_frame,
            text="应用到勾选商品",
            command=self._apply_condition_to_checked,
            state="disabled",
        )
        self._apply_condition_checked_btn.pack(side="left", padx=2, pady=6)
        ttk.Label(condition_frame, textvariable=self._condition_hint_var, foreground="gray").pack(
            side="left", padx=10, pady=6
        )

        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="both", expand=True, padx=8, pady=4)

        columns = [
            ("selected", "勾选", 56),
            ("account", "账号", 90),
            ("qc_code", "质检码", 110),
            ("imei", "IMEI", 120),
            ("title", "商品名", 220),
            ("condition", "成色", 90),
            ("status", "状态", 90),
            ("current_price", "当前价", 80),
            ("suggested_price", "建议价", 80),
            ("diff", "差价", 70),
            ("settle_price", "预计结算价", 95),
            ("cost_price", "成本价", 80),
            ("listed_days", "在架天", 70),
            ("op_status", "处理状态", 90),
            ("op_message", "结果", 240),
        ]

        vsb = ttk.Scrollbar(tree_frame, orient="vertical")
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal")
        self._tree = ttk.Treeview(
            tree_frame,
            columns=[c[0] for c in columns],
            show="headings",
            selectmode="browse",
            yscrollcommand=vsb.set,
            xscrollcommand=hsb.set,
        )
        vsb.config(command=self._tree.yview)
        hsb.config(command=self._tree.xview)

        for key, label, width in columns:
            self._tree.heading(key, text=label)
            anchor = "center" if key in {"selected", "status", "current_price", "suggested_price", "diff", "settle_price", "cost_price", "listed_days", "op_status"} else "w"
            self._tree.column(key, width=width, minwidth=50, anchor=anchor)

        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self._tree.bind("<<TreeviewSelect>>", lambda _event: self._on_tree_select())
        self._tree.bind("<Button-1>", self._on_tree_click, add="+")

        self._tree.tag_configure("ok", background="#f2f8f2")
        self._tree.tag_configure("fail", background="#fff1f1")
        self._tree.tag_configure("warn", background="#fff8e8")
        self._tree.tag_configure("idle", background="#f9fbff")

        preview_frame = ttk.LabelFrame(self, text="定价预览 / 调价逻辑")
        preview_frame.pack(fill="both", padx=8, pady=(0, 6))
        self._preview_text = tk.Text(preview_frame, height=8, wrap="word", state="disabled", font=("Consolas", 9))
        self._preview_text.pack(fill="both", expand=True, padx=6, pady=6)

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=8, pady=(0, 6))
        self._progress = ttk.Progressbar(bottom, mode="indeterminate")
        self._progress.pack(fill="x", side="top", pady=(0, 4))
        ttk.Label(bottom, textvariable=self._status_var, foreground="gray").pack(anchor="w")

    def _handle_close(self):
        if callable(self._on_close_callback):
            try:
                self._on_close_callback()
            except Exception:
                pass
        self.destroy()

    def _notify_store_updated(self):
        if self._event_target is None:
            return
        for event_name in (self.IMPORTED_STORE_UPDATED_EVENT, "<<ImportedStoreUpdated>>"):
            try:
                self._event_target.event_generate(event_name)
            except Exception:
                pass

    def _set_busy(self, busy: bool, status: str = ""):
        self._busy = busy
        if busy:
            self._progress.start(10)
        else:
            self._progress.stop()
        if status:
            self._status_var.set(status)

    def _refresh_account_options(self):
        accounts = [account.name for account in self.ctx.account_store.enabled_accounts()]
        self._account_combo["values"] = accounts
        current = self._import_account_var.get().strip()
        if current in accounts:
            return
        self._import_account_var.set(accounts[0] if accounts else "")

    def _parse_import_codes(self) -> list[str]:
        raw = self._import_text.get("1.0", "end").strip()
        if not raw:
            return []
        parts = re.split(r"[\s,，;；、]+", raw)
        codes: list[str] = []
        seen: set[str] = set()
        for part in parts:
            code = str(part or "").strip()
            if not code or code in seen:
                continue
            seen.add(code)
            codes.append(code)
        return codes

    def _normalize_condition(self, value: str) -> str:
        return str(value or "").strip()

    def _resolve_condition(self, current: str, incoming: str) -> str:
        current_text = self._normalize_condition(current)
        incoming_text = self._normalize_condition(incoming)
        if current_text and current_text not in {"未知", "未知成色", "-"}:
            return current_text
        return incoming_text or current_text

    def _dedupe_values(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    def _detail_to_batch_item(self, detail, account_name: str) -> BatchItem:
        return BatchItem(
            product_id=detail.product_id,
            qc_code=detail.qc_code,
            title=detail.title,
            current_price=detail.current_price,
            status=detail.status,
            account_name=account_name,
            imei=getattr(detail, "imei", "") or "",
            model=detail.model,
            condition=detail.condition,
            capacity=detail.capacity,
            color=detail.color,
            cost_price=self.ctx.cost_map.get(detail.product_id) or getattr(detail, "cost_price", 0.0),
            listed_time=detail.listed_time,
            settle_price=detail.settle_price,
            import_source="manual",
            listing_eligible=detail.status == ProductStatus.NOT_LISTED,
            op_status="已导入",
            op_message="手动批量导入",
        )

    def _import_codes(self):
        codes = self._parse_import_codes()
        if not codes:
            messagebox.showinfo("提示", "请先输入 IMEI 或质检码", parent=self)
            return
        account_name = self._import_account_var.get().strip()
        if not account_name:
            messagebox.showinfo("提示", "请先选择操作店铺", parent=self)
            return
        if self._busy:
            return
        self._set_busy(True, f"正在导入 {len(codes)} 个编码 …")
        threading.Thread(target=self._do_import_codes, args=(account_name, codes), daemon=True).start()

    def _do_import_codes(self, account_name: str, codes: list[str]):
        accounts = {account.name: account for account in self.ctx.account_store.enabled_accounts()}
        account = accounts.get(account_name)
        if account is None:
            self.after(0, lambda: self._after_operation("导入失败：账号不存在或未启用"))
            return

        svc = ImeiService(account.name, account.cookie)
        found: dict[str, BatchItem] = {}
        ok = fail = refreshed = 0
        total = len(codes)
        batch_size = 50
        completed = 0

        try:
            for start in range(0, total, batch_size):
                batch = codes[start:start + batch_size]
                details, missing = svc.fetch_by_codes(batch)
                ok += len(batch) - len(missing)
                fail += len(missing)
                completed += len(batch)

                for detail in details:
                    item = self._detail_to_batch_item(detail, account.name)
                    existing = self.ctx.imported_store.get(item.product_id)
                    if existing is not None:
                        self.ctx.imported_store.update_item(
                            item.product_id,
                            qc_code=item.qc_code,
                            title=item.title,
                            current_price=item.current_price,
                            status=item.status,
                            account_name=item.account_name,
                            imei=item.imei,
                            model=item.model,
                            condition=self._resolve_condition(existing.condition, item.condition),
                            capacity=item.capacity,
                            color=item.color,
                            cost_price=item.cost_price,
                            listed_time=item.listed_time,
                            settle_price=item.settle_price,
                            suggested_settle_price=existing.suggested_settle_price,
                            selected=getattr(existing, "selected", True),
                            import_source=item.import_source or getattr(existing, "import_source", ""),
                            listing_eligible=item.listing_eligible,
                            op_status="已刷新",
                            op_message="手动批量导入刷新",
                        )
                        refreshed += 1
                    elif item.product_id not in found:
                        found[item.product_id] = item

                self.after(0, lambda i=completed, t=total: self._status_var.set(f"正在导入 {i}/{t} …"))
        except Exception as e:
            self.after(0, lambda err=str(e): self._after_operation(f"导入失败：{err}"))
            return

        added = self.ctx.imported_store.extend(list(found.values())) if found else 0
        if added > 0:
            self.after(0, lambda: self._import_text.delete("1.0", "end"))
        summary = f"批量导入完成：成功 {ok}，新增 {added}，刷新 {refreshed}，失败 {fail}"
        self.after(0, lambda s=summary: self._after_operation(s))

    def _current_preview_item(self) -> BatchItem | None:
        selected = self._tree.selection()
        if not selected:
            return None
        return self.ctx.imported_store.get(str(selected[0]))

    def _checked_items(self, visible_only: bool = False) -> list[BatchItem]:
        source = self._filtered_items() if visible_only else self.ctx.imported_store.get_all()
        return [item for item in source if getattr(item, "selected", True)]

    def _sync_selected_label(self):
        visible_items = self._filtered_items()
        visible_checked = sum(1 for item in visible_items if getattr(item, "selected", True))
        total_checked = sum(1 for item in self.ctx.imported_store.get_all() if getattr(item, "selected", True))
        self._selected_var.set(f"已勾选 {visible_checked}/{len(visible_items)}（总 {total_checked}）")

    def _set_checked_for_items(self, items: list[BatchItem], checked: bool, refresh: bool = True):
        updated = 0
        for item in items:
            if self.ctx.imported_store.update_item(item.product_id, selected=checked):
                updated += 1
        if refresh and updated:
            self._render()
        return updated

    def _check_all_visible(self):
        items = self._filtered_items()
        if not items:
            return
        updated = self._set_checked_for_items(items, True)
        if updated:
            self._status_var.set(f"已勾选当前显示的 {updated} 件商品")
            self._notify_store_updated()

    def _uncheck_all_visible(self):
        items = self._filtered_items()
        if not items:
            return
        updated = self._set_checked_for_items(items, False)
        if updated:
            self._status_var.set(f"已取消当前显示的 {updated} 件商品勾选")
            self._notify_store_updated()

    def _invert_visible_checked(self):
        items = self._filtered_items()
        if not items:
            return
        updated = 0
        for item in items:
            if self.ctx.imported_store.update_item(item.product_id, selected=not getattr(item, "selected", True)):
                updated += 1
        if updated:
            self._render()
            self._status_var.set(f"已反选当前显示的 {updated} 件商品")
            self._notify_store_updated()

    def _toggle_checked(self, product_id: str):
        item = self.ctx.imported_store.get(str(product_id))
        if item is None:
            return
        checked = not getattr(item, "selected", True)
        if self.ctx.imported_store.update_item(item.product_id, selected=checked):
            self._render()
            self._status_var.set(f"已{'勾选' if checked else '取消勾选'}商品：{item.qc_code or item.title or item.product_id}")
            self._notify_store_updated()

    def _matches_status_filter(self, item: BatchItem) -> bool:
        selected = self._status_filter_var.get().strip() or "全部"
        if selected == "全部":
            return True
        status = getattr(item, "status", None)
        label = getattr(status, "label", "")
        if label:
            return label == selected
        raw = str(getattr(status, "value", status) or "").strip()
        mapping = {
            ProductStatus.ON_SALE.value: "在售",
            ProductStatus.NOT_LISTED.value: "未上架",
            ProductStatus.SOLD.value: "已售",
            ProductStatus.OFF_SALE.value: "已下架",
            ProductStatus.IN_QC.value: "质检中",
            ProductStatus.UNKNOWN.value: "未知",
        }
        return mapping.get(raw, "未知") == selected

    def _filtered_items(self):
        items = self.ctx.imported_store.get_all()
        items = [item for item in items if self._matches_status_filter(item)]
        keyword = self._filter_var.get().strip().lower()
        if keyword:
            items = [
                item for item in items
                if keyword in str(item.qc_code or "").lower()
                or keyword in str(getattr(item, "imei", "") or "").lower()
                or keyword in str(item.title or "").lower()
                or keyword in str(item.model or "").lower()
                or keyword in str(item.account_name or "").lower()
                or keyword in str(item.condition or "").lower()
            ]
        return items[:self.PAGE_SIZE]

    def _status_label(self, item: BatchItem) -> str:
        status = getattr(item, "status", None)
        label = getattr(status, "label", "")
        if label and label != "未知":
            return label
        raw = getattr(status, "value", status)
        return str(raw or "—")

    def _condition_options_for(self, item: BatchItem) -> list[str]:
        options: list[str] = []
        try:
            filtered = self.ctx.sold_cache.get_filter_options(
                model=item.model or item.title or "",
                capacity=item.capacity or "",
                color=item.color or "",
                fuzzy_model=True,
            )
            options.extend(filtered.get("condition", []))
            if not options:
                broader = self.ctx.sold_cache.get_filter_options(
                    model=item.model or item.title or "",
                    fuzzy_model=True,
                )
                options.extend(broader.get("condition", []))
        except Exception:
            pass
        current = self._normalize_condition(item.condition)
        return self._dedupe_values([current, *options, "未知成色"])

    def _update_condition_editor(self):
        item = self._current_preview_item()
        checked_items = self._checked_items()
        if item is None:
            self._condition_combo.configure(state="disabled")
            self._condition_combo["values"] = ()
            self._condition_var.set("")
            self._apply_condition_current_btn.configure(state="disabled")
            self._apply_condition_checked_btn.configure(state="normal" if checked_items else "disabled")
            self._condition_hint_var.set("先在表格里点中 1 件商品，再调整成色。")
            return

        options = self._condition_options_for(item)
        self._condition_combo["values"] = options
        self._condition_var.set(self._normalize_condition(item.condition) or (options[0] if options else ""))
        self._condition_combo.configure(state="normal")
        self._apply_condition_current_btn.configure(state="normal")
        self._apply_condition_checked_btn.configure(state="normal" if checked_items else "disabled")
        self._condition_hint_var.set(
            f"当前预览：{item.model or item.title or '—'} / 成色 {item.condition or '—'} / 容量 {item.capacity or '—'} / 颜色 {item.color or '—'}"
        )

    def _apply_condition(self, items: list[BatchItem], scope_label: str):
        condition = self._normalize_condition(self._condition_var.get())
        if not items:
            messagebox.showinfo("提示", f"请先{scope_label}", parent=self)
            return
        if not condition:
            messagebox.showinfo("提示", "请先选择或输入成色", parent=self)
            return
        updated = 0
        for item in items:
            if self.ctx.imported_store.update_item(
                item.product_id,
                condition=condition,
                pricing=None,
                suggested_price=None,
                suggested_settle_price=None,
                new_price=None,
                reprice_ok=None,
                reprice_msg="",
                op_status="待匹配",
                op_message=f"成色已修正为 {condition}，请重新匹配建议价",
            ):
                updated += 1
        if updated:
            self._render()
            self._status_var.set(f"已更新 {updated} 件商品成色为：{condition}")
            self._notify_store_updated()

    def _apply_condition_to_current(self):
        item = self._current_preview_item()
        self._apply_condition([item] if item is not None else [], "点中 1 件商品")

    def _apply_condition_to_checked(self):
        self._apply_condition(self._checked_items(), "勾选商品")

    def _on_tree_select(self):
        self._update_condition_editor()
        self._update_preview()

    def _on_tree_click(self, event):
        region = self._tree.identify("region", event.x, event.y)
        column = self._tree.identify_column(event.x)
        if column != "#1":
            return None
        if region == "cell":
            iid = self._tree.identify_row(event.y)
            if iid:
                self._toggle_checked(iid)
                return "break"
        if region == "heading":
            visible = self._filtered_items()
            if visible:
                should_check = any(not getattr(item, "selected", True) for item in visible)
                self._set_checked_for_items(visible, should_check)
                self._status_var.set(f"已{'勾选' if should_check else '取消勾选'}当前显示商品")
                self._notify_store_updated()
                return "break"
        return None

    def _render_preview(self, text: str):
        self._preview_text.config(state="normal")
        self._preview_text.delete("1.0", "end")
        self._preview_text.insert("1.0", text)
        self._preview_text.config(state="disabled")

    def _update_preview(self):
        item = self._current_preview_item()
        if item is None:
            self._render_preview("单击表格中的 1 行可刷新该商品的建议价预览；勾选商品后可批量执行“匹配建议价”。两者都只更新建议价/预览，不会直接改价或上架。")
            return
        current_settle = item.settle_price
        preview_settle = item.suggested_settle_price if item.suggested_settle_price is not None else item.settle_price
        lines = [
            f"账号: {item.account_name or '—'}",
            f"质检码: {item.qc_code or '—'}",
            f"IMEI: {getattr(item, 'imei', '') or '—'}",
            f"商品: {item.title or '—'}",
            f"型号: {item.model or '—'}",
            f"成色: {item.condition or '—'}",
            f"容量: {item.capacity or '—'}",
            f"颜色: {item.color or '—'}",
            f"状态: {self._status_label(item)}",
            f"当前价: ¥{item.current_price:,.0f}",
            f"当前结算价: {'¥%s' % format(current_settle, ',.0f') if current_settle else '—'}",
            f"建议价: {'¥%s' % format(item.suggested_price, ',.0f') if item.suggested_price is not None else '—'}",
            f"建议结算价: {'¥%s' % format(preview_settle, ',.0f') if preview_settle else '—'}",
            f"成本价: {'¥%s' % format(item.cost_price, ',.0f') if item.cost_price else '—'}",
            f"处理状态: {item.op_status or '待处理'}",
            f"处理结果: {item.op_message or item.reprice_msg or '—'}",
            "",
            "提示: 可点“当前行预览”刷新当前商品建议价；批量“匹配建议价”仅更新勾选商品的建议价/预览，不会直接改价或上架。",
            "",
            "定价预览:",
            _pricing_preview(item.pricing, self.ctx.rule_engine, item) if item.pricing is not None else "暂无定价结果，请点“当前行预览”或对勾选商品执行“匹配建议价”。",
        ]
        self._render_preview("\n".join(lines))

    def _row_tag(self, item: BatchItem) -> tuple[str, ...]:
        if item.reprice_ok is True:
            return ("ok",)
        if item.reprice_ok is False:
            return ("fail",)
        if item.suggested_price is not None and abs(item.suggested_price - item.current_price) >= 100:
            return ("warn",)
        return ("idle",)

    def _render(self):
        items = self._filtered_items()
        focused = self._tree.focus()
        selected = self._tree.selection()
        for row in self._tree.get_children():
            self._tree.delete(row)

        for item in items:
            diff = ""
            if item.suggested_price is not None:
                diff = f"{item.suggested_price - item.current_price:+.0f}"
            settle = item.settle_price
            suggested_settle = item.suggested_settle_price if item.suggested_settle_price is not None else settle
            self._tree.insert(
                "",
                "end",
                iid=str(item.product_id),
                values=(
                    "☑" if getattr(item, "selected", True) else "☐",
                    item.account_name,
                    item.qc_code,
                    getattr(item, "imei", "") or "—",
                    item.title[:40],
                    item.condition or "—",
                    self._status_label(item),
                    f"¥{item.current_price:,.0f}",
                    f"¥{item.suggested_price:,.0f}" if item.suggested_price is not None else "—",
                    diff or "—",
                    f"¥{suggested_settle:,.0f}" if suggested_settle else "—",
                    f"¥{item.cost_price:,.0f}" if item.cost_price else "—",
                    f"{item.stale_days}天" if item.listed_time else "—",
                    item.op_status or "待处理",
                    item.op_message or item.reprice_msg or "—",
                ),
                tags=self._row_tag(item),
            )

        existing_rows = set(self._tree.get_children())
        next_focus = None
        for candidate in (focused, *selected):
            if candidate in existing_rows:
                next_focus = candidate
                break
        if next_focus is not None:
            self._tree.focus(next_focus)
            self._tree.selection_set(next_focus)

        total = self.ctx.imported_store.count()
        shown = len(items)
        self._summary_var.set(f"已导入 {total} 件商品，当前显示 {shown} 件")
        self._sync_selected_label()
        self._update_condition_editor()
        self._update_preview()

    def _refresh_selected(self):
        items = self._checked_items()
        if not items:
            messagebox.showinfo("提示", "请先勾选商品", parent=self)
            return
        if self._busy:
            return
        self._set_busy(True, f"正在刷新 {len(items)} 件勾选商品 …")
        threading.Thread(target=self._do_refresh_selected, args=(items,), daemon=True).start()

    def _do_refresh_selected(self, items: list[BatchItem]):
        accounts = {account.name: account for account in self.ctx.account_store.enabled_accounts()}
        services: dict[str, ImeiService] = {}
        ok = fail = 0

        for item in items:
            account = accounts.get(item.account_name)
            if account is None:
                self.ctx.imported_store.update_item(item.product_id, op_status="失败", op_message="账号不存在或未启用", reprice_ok=False)
                fail += 1
                continue
            svc = services.get(account.name)
            if svc is None:
                svc = ImeiService(account.name, account.cookie)
                services[account.name] = svc
            try:
                detail = _refresh_imported_detail(svc, item)
                if detail is None:
                    self.ctx.imported_store.update_item(item.product_id, op_status="失败", op_message="未找到商品", reprice_ok=False)
                    fail += 1
                    continue
                self.ctx.imported_store.update_item(
                    item.product_id,
                    qc_code=detail.qc_code or item.qc_code,
                    current_price=detail.current_price,
                    title=detail.title,
                    model=detail.model,
                    condition=self._resolve_condition(item.condition, detail.condition),
                    capacity=detail.capacity,
                    color=detail.color,
                    listed_time=detail.listed_time,
                    settle_price=detail.settle_price,
                    suggested_settle_price=item.suggested_settle_price,
                    status=detail.status,
                    import_source=getattr(item, "import_source", "manual") or "manual",
                    listing_eligible=detail.status == ProductStatus.NOT_LISTED,
                    op_status="已刷新",
                    op_message="商品信息已刷新",
                    reprice_ok=None,
                    imei=getattr(detail, "imei", "") or getattr(item, "imei", ""),
                    selected=getattr(item, "selected", True),
                )
                ok += 1
            except Exception as e:
                self.ctx.imported_store.update_item(item.product_id, op_status="失败", op_message=str(e), reprice_ok=False)
                fail += 1

        self.after(0, lambda o=ok, f=fail: self._after_operation(f"刷新完成：成功 {o}，失败 {f}"))

    def _match_selected(self):
        items = self._checked_items()
        if not items:
            messagebox.showinfo("提示", "请先勾选商品", parent=self)
            return
        if self._busy:
            return
        self._run_match_items(items, status_text=f"正在匹配 {len(items)} 件勾选商品建议价 …", finish_text="匹配完成")

    def _match_current_item(self):
        item = self._current_preview_item()
        if item is None:
            messagebox.showinfo("提示", "请先在表格里单击选中 1 件商品", parent=self)
            return
        if self._busy:
            return
        label = item.qc_code or item.title or item.product_id
        self._run_match_items([item], status_text=f"正在刷新当前商品预览：{label} …", finish_text="当前商品预览已刷新")

    def _run_match_items(self, items: list[BatchItem], *, status_text: str, finish_text: str):
        self._set_busy(True, status_text)
        threading.Thread(target=self._do_match_items, args=(items, finish_text), daemon=True).start()

    def _fetch_suggested_settle_price(self, account_name: str, product_id: str, price: float):
        if not account_name or not product_id or price <= 0:
            return None
        accounts = {account.name: account for account in self.ctx.account_store.enabled_accounts()}
        account = accounts.get(account_name)
        if account is None:
            return None
        try:
            svc = ImeiService(account.name, account.cookie)
            return svc.estimate_settle_price(product_id, price)
        except Exception:
            return None

    def _do_match_selected(self, items: list[BatchItem]):
        self._do_match_items(items, "匹配完成")

    def _do_match_items(self, items: list[BatchItem], finish_text: str):
        matched = skipped = 0
        for item in items:
            pipeline = run_reprice_pipeline(
                item,
                self.ctx.sold_cache,
                self.ctx.rule_engine,
                apply_rules=True,
                settle_price_estimator=lambda price, account_name=item.account_name, product_id=item.product_id: self._fetch_suggested_settle_price(
                    account_name,
                    product_id,
                    price,
                ),
            )
            pricing = pipeline["pricing"]
            decision = pipeline["decision"]
            new_price = pipeline.get("suggested_price")
            settle = pipeline.get("suggested_settle_price")
            preview = pipeline["preview"]
            if new_price is None:
                self.ctx.imported_store.update_item(
                    item.product_id,
                    pricing=pricing,
                    suggested_price=None,
                    floor_price=pricing.floor_price,
                    suggested_settle_price=None,
                    confidence=getattr(pricing.confidence, "value", pricing.confidence),
                    rule_hit=decision.get("rule_hit", ""),
                    op_status="跳过",
                    op_message=f"无定价依据｜{preview}",
                    reprice_ok=None,
                )
                skipped += 1
                continue
            self.ctx.imported_store.update_item(
                item.product_id,
                pricing=pricing,
                suggested_price=new_price,
                floor_price=pricing.floor_price,
                suggested_settle_price=settle,
                confidence=getattr(pricing.confidence, "value", pricing.confidence),
                rule_hit=decision.get("rule_hit", ""),
                op_status="已匹配",
                op_message=f"建议价预览已刷新｜{preview}",
                reprice_ok=None,
            )
            matched += 1

        self.after(0, lambda m=matched, s=skipped, text=finish_text: self._after_operation(f"{text}：成功 {m}，跳过 {s}"))

    def _apply_selected(self):
        items = [
            item for item in self._checked_items()
            if item.suggested_price is not None and abs(item.suggested_price - item.current_price) >= 1
        ]
        if not items:
            messagebox.showinfo("提示", "勾选商品里没有需要执行的建议价", parent=self)
            return
        if not messagebox.askyesno("确认", f"即将对 {len(items)} 件勾选商品执行改价/上架，确认？", parent=self):
            return
        if self._busy:
            return
        self._set_busy(True, f"正在执行 {len(items)} 件勾选商品 …")
        threading.Thread(target=self._do_apply_selected, args=(items,), daemon=True).start()

    def _do_apply_selected(self, items: list[BatchItem]):
        accounts = {account.name: account for account in self.ctx.account_store.enabled_accounts()}
        services: dict[str, ImeiService] = {}
        ok = fail = 0

        for item in items:
            account = accounts.get(item.account_name)
            if account is None:
                self.ctx.imported_store.update_item(item.product_id, op_status="失败", op_message="账号不存在或未启用", reprice_ok=False)
                fail += 1
                continue
            svc = services.get(account.name)
            if svc is None:
                svc = ImeiService(account.name, account.cookie)
                services[account.name] = svc

            try:
                detail = _refresh_imported_detail(svc, item)
            except Exception as e:
                self.ctx.imported_store.update_item(item.product_id, op_status="失败", op_message=str(e), reprice_ok=False)
                fail += 1
                continue

            if detail is None:
                self.ctx.imported_store.update_item(item.product_id, op_status="失败", op_message="未找到商品", reprice_ok=False)
                fail += 1
                continue

            new_price = round(item.suggested_price or 0, 0)
            success, msg = svc.change_price(detail, new_price)
            if success:
                settle = PricingEngine.calc_settle_price(new_price)
                refreshed = _refresh_imported_detail(svc, item) or detail
                old_price = item.current_price
                self.ctx.history_db.record(PriceChangeRecord(
                    id=None,
                    timestamp=datetime.datetime.now(),
                    product_id=item.product_id,
                    qc_code=item.qc_code,
                    title=item.title,
                    model=item.model,
                    condition=item.condition,
                    capacity=item.capacity,
                    color=item.color,
                    old_price=old_price,
                    new_price=new_price,
                    diff=new_price - old_price,
                    settle_price=settle or 0.0,
                    trigger=PriceTrigger.MANUAL,
                    account_name=item.account_name,
                    rule_hit=item.rule_hit,
                ))
                preview = _pricing_preview(item.pricing, self.ctx.rule_engine, item) if item.pricing is not None else ""
                success_msg = msg or "执行成功"
                self.ctx.imported_store.update_item(
                    item.product_id,
                    qc_code=getattr(refreshed, "qc_code", item.qc_code) or item.qc_code,
                    title=getattr(refreshed, "title", item.title),
                    model=getattr(refreshed, "model", item.model),
                    condition=self._resolve_condition(item.condition, getattr(refreshed, "condition", "")),
                    capacity=getattr(refreshed, "capacity", item.capacity),
                    color=getattr(refreshed, "color", item.color),
                    current_price=getattr(refreshed, "current_price", new_price),
                    settle_price=getattr(refreshed, "settle_price", settle) or settle,
                    suggested_settle_price=getattr(refreshed, "settle_price", settle) or settle,
                    listed_time=getattr(refreshed, "listed_time", item.listed_time),
                    status=getattr(refreshed, "status", item.status),
                    new_price=new_price,
                    suggested_price=new_price,
                    reprice_ok=True,
                    reprice_msg=success_msg,
                    op_status="已完成",
                    op_message=f"{success_msg}{'｜' + preview if preview else ''}",
                    imei=getattr(refreshed, "imei", "") or getattr(item, "imei", ""),
                    selected=getattr(item, "selected", True),
                )
                ok += 1
            else:
                self.ctx.imported_store.update_item(
                    item.product_id,
                    new_price=new_price,
                    reprice_ok=False,
                    reprice_msg=msg,
                    op_status="失败",
                    op_message=msg,
                )
                fail += 1

        self.after(0, lambda o=ok, f=fail: self._after_operation(f"执行完成：成功 {o}，失败 {f}"))

    def _remove_selected(self):
        items = self._checked_items()
        if not items:
            messagebox.showinfo("提示", "请先勾选商品", parent=self)
            return
        if not messagebox.askyesno("确认", f"确认移除 {len(items)} 件勾选商品？", parent=self):
            return
        removed = 0
        for item in items:
            if self.ctx.imported_store.remove(item.product_id):
                removed += 1
        self._render()
        self._status_var.set(f"已移除 {removed} 件商品")
        self._notify_store_updated()

    def _clear_all(self):
        total = self.ctx.imported_store.count()
        if total <= 0:
            messagebox.showinfo("提示", "当前没有已导入商品", parent=self)
            return
        if not messagebox.askyesno("确认", f"确认清空全部 {total} 件导入商品？", parent=self):
            return
        self.ctx.imported_store.clear()
        self._render()
        self._status_var.set("已清空导入商品列表")
        self._notify_store_updated()

    def _after_operation(self, status: str):
        self._set_busy(False, status)
        self._render()
        self._notify_store_updated()
