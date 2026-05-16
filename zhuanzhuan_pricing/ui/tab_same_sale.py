# -*- coding: utf-8 -*-
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .app import AppContext

from ..services.same_sale_service import SameSaleListing


class SameSaleTab:
    def __init__(self, parent: ttk.Notebook, ctx: "AppContext"):
        self.ctx = ctx
        self.frame = ttk.Frame(parent)
        self._event_target = parent
        self._groups: list = []
        self._selected_group_id: str = ""
        self._selected_listing_key: tuple[str, str, str, str] | None = None
        parent.add(self.frame, text="🔗 同售管理")
        self._build_ui()
        self._bind_events()
        self.refresh()

    def _build_ui(self):
        intro = ttk.LabelFrame(self.frame, text="同售管理")
        intro.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(
            intro,
            text="用于维护同一台机器在多个平台的挂单关系。当前先提供本地台账：手动建组、同步转转导入商品、标记某平台已售并生成其他平台待下架事项。",
            foreground="gray",
            justify="left",
            wraplength=1180,
        ).pack(anchor="w", padx=10, pady=(10, 8))

        toolbar = ttk.Frame(intro)
        toolbar.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(toolbar, text="🔄 刷新", command=self.refresh).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="📥 从导入商品同步转转", command=self._sync_imported).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="➕ 新建分组", command=self._create_group).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="➕ 添加挂单", command=self._add_listing).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="✏️ 编辑备注", command=self._edit_group_note).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="🗑️ 删除分组", command=self._delete_group).pack(side="left")

        stats = ttk.Frame(self.frame)
        stats.pack(fill="x", padx=8, pady=(0, 4))
        self._stats_var = tk.StringVar(value="同售分组：0 | 已售：0 | 待下架：0")
        ttk.Label(stats, textvariable=self._stats_var, foreground="gray").pack(anchor="w")

        content = ttk.PanedWindow(self.frame, orient="horizontal")
        content.pack(fill="both", expand=True, padx=8, pady=4)

        left = ttk.LabelFrame(content, text="同售分组")
        right = ttk.LabelFrame(content, text="平台挂单")
        content.add(left, weight=3)
        content.add(right, weight=5)

        group_cols = [
            ("group_id", "分组ID", 150),
            ("machine_code", "质检码", 110),
            ("imei", "IMEI", 130),
            ("model", "型号", 150),
            ("platforms", "挂单数", 70),
            ("pending", "待下架", 70),
            ("updated_at", "更新时间", 140),
        ]
        self._group_tree = ttk.Treeview(left, columns=[c[0] for c in group_cols], show="headings", height=16)
        for col_id, heading, width in group_cols:
            self._group_tree.heading(col_id, text=heading)
            self._group_tree.column(col_id, width=width, minwidth=50, anchor="w")
        group_vsb = ttk.Scrollbar(left, orient="vertical", command=self._group_tree.yview)
        self._group_tree.configure(yscrollcommand=group_vsb.set)
        self._group_tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        group_vsb.pack(side="right", fill="y", padx=(0, 6), pady=6)
        self._group_tree.bind("<<TreeviewSelect>>", self._on_group_select)

        listing_cols = [
            ("platform_label", "平台", 80),
            ("account_name", "账号", 90),
            ("product_id", "商品ID", 110),
            ("qc_code", "质检码", 110),
            ("imei", "IMEI", 130),
            ("status", "状态", 90),
            ("price", "价格", 80),
            ("todo", "待办", 80),
            ("updated_at", "更新时间", 140),
        ]
        self._listing_tree = ttk.Treeview(right, columns=[c[0] for c in listing_cols], show="headings", height=16)
        for col_id, heading, width in listing_cols:
            self._listing_tree.heading(col_id, text=heading)
            self._listing_tree.column(col_id, width=width, minwidth=50, anchor="w")
        self._listing_tree.tag_configure("sold", background="#fce8e6")
        self._listing_tree.tag_configure("pending", background="#fff8e1")
        self._listing_tree.tag_configure("done", background="#e8f5e9")
        list_vsb = ttk.Scrollbar(right, orient="vertical", command=self._listing_tree.yview)
        self._listing_tree.configure(yscrollcommand=list_vsb.set)
        self._listing_tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        list_vsb.pack(side="right", fill="y", padx=(0, 6), pady=6)
        self._listing_tree.bind("<<TreeviewSelect>>", self._on_listing_select)

        actions = ttk.LabelFrame(self.frame, text="挂单操作")
        actions.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Button(actions, text="✅ 标记已售", command=self._mark_sold).pack(side="left", padx=6, pady=6)
        ttk.Button(actions, text="📦 标记已下架", command=self._mark_delisted).pack(side="left", padx=6, pady=6)
        ttk.Button(actions, text="↺ 清除待下架", command=self._clear_pending).pack(side="left", padx=6, pady=6)

        self._detail_var = tk.StringVar(value="请选择左侧同售分组。")
        ttk.Label(self.frame, textvariable=self._detail_var, foreground="gray", justify="left").pack(fill="x", padx=10, pady=(0, 6))

    def _bind_events(self):
        return

    def refresh(self):
        self._groups = self.ctx.same_sale_store.get_all()
        self._render_groups()
        self._update_stats()
        self._render_details()

    def _update_stats(self):
        self._stats_var.set(
            f"同售分组：{self.ctx.same_sale_store.count()} | 已售：{self.ctx.same_sale_store.sold_count()} | 待下架：{self.ctx.same_sale_store.pending_count()}"
        )

    def _render_groups(self):
        selected = self._selected_group_id
        for item in self._group_tree.get_children():
            self._group_tree.delete(item)

        for group in self._groups:
            pending = sum(1 for listing in group.listings if listing.delist_pending)
            values = (
                group.group_id,
                group.machine_code or "—",
                group.imei or "—",
                group.model or "—",
                len(group.listings),
                pending,
                (group.updated_at or "")[:16].replace("T", " "),
            )
            item_id = self._group_tree.insert("", "end", values=values)
            if group.group_id == selected:
                self._group_tree.selection_set(item_id)
                self._group_tree.focus(item_id)

        if not selected and self._groups:
            first = self._group_tree.get_children()
            if first:
                self._group_tree.selection_set(first[0])
                self._on_group_select()

    def _selected_group(self):
        for group in self._groups:
            if group.group_id == self._selected_group_id:
                return group
        return None

    def _render_details(self):
        group = self._selected_group()
        for item in self._listing_tree.get_children():
            self._listing_tree.delete(item)
        self._selected_listing_key = None
        if group is None:
            self._detail_var.set("请选择左侧同售分组。")
            return

        lines = [
            f"分组：{group.group_id}",
            f"机器标识：质检码 {group.machine_code or '—'} / IMEI {group.imei or '—'}",
            f"机型：{group.model or '—'} {group.condition or ''} {group.capacity or ''} {group.color or ''}".strip(),
            f"备注：{group.note or '—'}",
        ]
        self._detail_var.set(" | ".join(lines))

        for listing in group.listings:
            todo = "待下架" if listing.delist_pending else ("已完成" if listing.delisted else "—")
            values = (
                listing.platform_label or listing.platform,
                listing.account_name or "—",
                listing.product_id or "—",
                listing.qc_code or "—",
                listing.imei or "—",
                listing.status or "—",
                f"¥{listing.price:,.0f}" if listing.price else "—",
                todo,
                (listing.updated_at or "")[:16].replace("T", " "),
            )
            tags = ()
            if listing.sold:
                tags = ("sold",)
            elif listing.delist_pending:
                tags = ("pending",)
            elif listing.delisted:
                tags = ("done",)
            item_id = self._listing_tree.insert("", "end", values=values, tags=tags)
            self._listing_tree.item(item_id, tags=tags)

    def _on_group_select(self, _event=None):
        sel = self._group_tree.selection()
        if not sel:
            return
        values = self._group_tree.item(sel[0], "values")
        self._selected_group_id = values[0] if values else ""
        self._render_details()

    def _on_listing_select(self, _event=None):
        sel = self._listing_tree.selection()
        group = self._selected_group()
        if not sel or group is None:
            self._selected_listing_key = None
            return
        values = self._listing_tree.item(sel[0], "values")
        if not values:
            self._selected_listing_key = None
            return
        platform_label, _account_name, product_id, qc_code, imei, *_rest = values
        self._selected_listing_key = (str(platform_label), str(product_id), str(qc_code), str(imei))

    def _selected_listing(self):
        group = self._selected_group()
        key = self._selected_listing_key
        if group is None or key is None:
            return None
        platform_label, product_id, qc_code, imei = key
        for listing in group.listings:
            if (listing.platform_label or listing.platform) != platform_label:
                continue
            if (listing.product_id or "—") == product_id and (listing.qc_code or "—") == qc_code and (listing.imei or "—") == imei:
                return listing
        return None

    def _create_group(self):
        machine_code = simpledialog.askstring("新建分组", "质检码（可留空）:", parent=self.frame.winfo_toplevel())
        if machine_code is None:
            return
        imei = simpledialog.askstring("新建分组", "IMEI（可留空）:", parent=self.frame.winfo_toplevel())
        if imei is None:
            return
        model = simpledialog.askstring("新建分组", "型号（可留空）:", parent=self.frame.winfo_toplevel()) or ""
        self.ctx.same_sale_store.add_group(machine_code=machine_code or "", imei=imei or "", model=model)
        self.refresh()

    def _add_listing(self):
        group = self._selected_group()
        if group is None:
            messagebox.showwarning("提示", "请先选择一个同售分组")
            return
        platform = simpledialog.askstring("添加挂单", "平台标识（如 zhuanzhuan / paipai / xianyu / 95fen）:", parent=self.frame.winfo_toplevel())
        if platform is None or not platform.strip():
            return
        platform_label = simpledialog.askstring("添加挂单", "平台显示名:", initialvalue=platform, parent=self.frame.winfo_toplevel())
        if platform_label is None:
            return
        product_id = simpledialog.askstring("添加挂单", "商品ID:", parent=self.frame.winfo_toplevel()) or ""
        qc_code = simpledialog.askstring("添加挂单", "质检码:", initialvalue=group.machine_code or "", parent=self.frame.winfo_toplevel()) or ""
        imei = simpledialog.askstring("添加挂单", "IMEI:", initialvalue=group.imei or "", parent=self.frame.winfo_toplevel()) or ""
        account_name = simpledialog.askstring("添加挂单", "账号名:", parent=self.frame.winfo_toplevel()) or ""
        title = simpledialog.askstring("添加挂单", "标题:", parent=self.frame.winfo_toplevel()) or ""
        self.ctx.same_sale_store.add_listing(
            group.group_id,
            SameSaleListing(
                platform=platform,
                platform_label=platform_label,
                product_id=product_id,
                qc_code=qc_code,
                imei=imei,
                account_name=account_name,
                title=title,
            ),
        )
        self.refresh()

    def _edit_group_note(self):
        group = self._selected_group()
        if group is None:
            messagebox.showwarning("提示", "请先选择一个同售分组")
            return
        note = simpledialog.askstring("编辑备注", "备注:", initialvalue=group.note or "", parent=self.frame.winfo_toplevel())
        if note is None:
            return
        self.ctx.same_sale_store.update_group_note(group.group_id, note)
        self.refresh()

    def _delete_group(self):
        group = self._selected_group()
        if group is None:
            messagebox.showwarning("提示", "请先选择一个同售分组")
            return
        if not messagebox.askyesno("删除分组", f"确认删除同售分组 {group.group_id}？"):
            return
        self.ctx.same_sale_store.delete_group(group.group_id)
        self._selected_group_id = ""
        self.refresh()

    def _sync_imported(self):
        items = self.ctx.zhuanzhuan.imported_store.get_all()
        if not items:
            messagebox.showinfo("同步", "暂无导入商品可同步")
            return
        count = 0
        for item in items:
            self.ctx.same_sale_store.sync_imported_item(item)
            count += 1
        self.refresh()
        messagebox.showinfo("同步完成", f"已同步 {count} 条转转导入商品到同售台账")

    def _listing_action_target(self) -> tuple[Optional[object], Optional[object]]:
        group = self._selected_group()
        listing = self._selected_listing()
        if group is None or listing is None:
            messagebox.showwarning("提示", "请先选择一个平台挂单")
            return None, None
        return group, listing

    def _mark_sold(self):
        group, listing = self._listing_action_target()
        if group is None or listing is None:
            return
        if not messagebox.askyesno("标记已售", f"确认把 {listing.platform_label or listing.platform} 挂单标记为已售，并把同组其他平台转为待下架？"):
            return
        self.ctx.same_sale_store.mark_sold(
            group.group_id,
            listing.platform,
            product_id=listing.product_id,
            qc_code=listing.qc_code,
            imei=listing.imei,
        )
        self.refresh()

    def _mark_delisted(self):
        group, listing = self._listing_action_target()
        if group is None or listing is None:
            return
        self.ctx.same_sale_store.mark_delisted(
            group.group_id,
            listing.platform,
            product_id=listing.product_id,
            qc_code=listing.qc_code,
            imei=listing.imei,
        )
        self.refresh()

    def _clear_pending(self):
        group, listing = self._listing_action_target()
        if group is None or listing is None:
            return
        self.ctx.same_sale_store.clear_pending(
            group.group_id,
            listing.platform,
            product_id=listing.product_id,
            qc_code=listing.qc_code,
            imei=listing.imei,
        )
        self.refresh()
