# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Iterable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..services.same_sale_service import SameSaleGroup, SameSaleListing

GROUP_COLUMNS = [
    ("group_id", "分组ID"),
    ("machine_code", "质检码"),
    ("imei", "IMEI"),
    ("model", "型号"),
    ("listing_count", "挂单数"),
    ("pending_count", "待下架"),
    ("updated_at", "更新时间"),
]

LISTING_COLUMNS = [
    ("platform_label", "平台"),
    ("account_name", "账号"),
    ("product_id", "商品ID"),
    ("qc_code", "质检码"),
    ("imei", "IMEI"),
    ("status", "状态"),
    ("price", "价格"),
    ("todo", "待办"),
    ("updated_at", "更新时间"),
]

STATUS_FILTER_OPTIONS = ["全部", "有待下架", "已有已售"]


def _normalize_text(value: str) -> str:
    return str(value or "").strip().lower()


def _group_matches_keyword(group: SameSaleGroup, keyword: str) -> bool:
    token = _normalize_text(keyword)
    if not token:
        return True
    haystacks = [group.group_id, group.model, group.machine_code, group.imei]
    for listing in group.listings:
        haystacks.extend([listing.qc_code, listing.imei, listing.product_id, listing.title])
    return any(token in _normalize_text(value) for value in haystacks)


def _group_matches_status(group: SameSaleGroup, status_filter: str) -> bool:
    if status_filter == "有待下架":
        return any(listing.delist_pending for listing in group.listings)
    if status_filter == "已有已售":
        return any(listing.sold for listing in group.listings)
    return True


def filter_groups(groups: Iterable[SameSaleGroup], keyword: str = "", status_filter: str = "全部") -> list[SameSaleGroup]:
    return [
        group for group in groups
        if _group_matches_keyword(group, keyword) and _group_matches_status(group, status_filter)
    ]


def listing_identity(listing: SameSaleListing) -> tuple[str, str, str, str]:
    return (
        str(listing.platform or "").strip().lower(),
        str(listing.product_id or "").strip(),
        str(listing.qc_code or "").strip(),
        str(listing.imei or "").strip(),
    )


def resolve_listing(group: SameSaleGroup | None, identity: tuple[str, str, str, str] | None) -> SameSaleListing | None:
    if group is None or identity is None:
        return None
    for listing in group.listings:
        if listing_identity(listing) == identity:
            return listing
    return None


class SameSaleQtTab(QWidget):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self._all_groups: list[SameSaleGroup] = []
        self._visible_groups: list[SameSaleGroup] = []
        self._selected_group_id = ""
        self._selected_listing_identity: tuple[str, str, str, str] | None = None
        self._last_action_message = ""
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        intro = QLabel(
            "同售管理 MVP：在主页内查看同售分组、搜索筛选、查看挂单详情，并支持标记已售/已下架。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(self._build_filter_box())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_group_box())
        splitter.addWidget(self._build_detail_box())
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)

        self._status_label = QLabel("加载中…")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

    def _build_filter_box(self):
        box = QGroupBox("筛选")
        layout = QGridLayout(box)

        layout.addWidget(QLabel("关键字"), 0, 0)
        self._keyword_input = QLineEdit()
        self._keyword_input.setPlaceholderText("搜索 group_id / 型号 / 质检码 / IMEI")
        self._keyword_input.returnPressed.connect(self._apply_filters)
        self._keyword_input.textChanged.connect(self._apply_filters)
        layout.addWidget(self._keyword_input, 0, 1)

        layout.addWidget(QLabel("状态"), 0, 2)
        self._status_filter = QComboBox()
        self._status_filter.addItems(STATUS_FILTER_OPTIONS)
        self._status_filter.currentIndexChanged.connect(self._apply_filters)
        layout.addWidget(self._status_filter, 0, 3)

        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.refresh)
        layout.addWidget(refresh_btn, 0, 4)
        layout.setColumnStretch(1, 1)
        return box

    def _build_group_box(self):
        box = QGroupBox("同售分组")
        layout = QVBoxLayout(box)

        self._group_table = QTableWidget(0, len(GROUP_COLUMNS))
        self._group_table.setHorizontalHeaderLabels([label for _, label in GROUP_COLUMNS])
        self._group_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._group_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._group_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._group_table.verticalHeader().setVisible(False)
        self._group_table.horizontalHeader().setStretchLastSection(True)
        self._group_table.setAlternatingRowColors(True)
        self._group_table.itemSelectionChanged.connect(self._on_group_selected)
        layout.addWidget(self._group_table, 1)

        return box

    def _build_detail_box(self):
        box = QGroupBox("分组详情")
        layout = QVBoxLayout(box)

        self._summary_label = QLabel("请选择左侧同售分组。")
        self._summary_label.setWordWrap(True)
        layout.addWidget(self._summary_label)

        action_row = QHBoxLayout()
        mark_sold_btn = QPushButton("标记已售")
        mark_delisted_btn = QPushButton("标记已下架")
        mark_sold_btn.clicked.connect(self._mark_sold)
        mark_delisted_btn.clicked.connect(self._mark_delisted)
        action_row.addWidget(mark_sold_btn)
        action_row.addWidget(mark_delisted_btn)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        self._listing_table = QTableWidget(0, len(LISTING_COLUMNS))
        self._listing_table.setHorizontalHeaderLabels([label for _, label in LISTING_COLUMNS])
        self._listing_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._listing_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._listing_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._listing_table.verticalHeader().setVisible(False)
        self._listing_table.horizontalHeader().setStretchLastSection(True)
        self._listing_table.setAlternatingRowColors(True)
        self._listing_table.itemSelectionChanged.connect(self._on_listing_selected)
        layout.addWidget(self._listing_table, 1)

        return box

    def refresh(self):
        self._all_groups = self.ctx.same_sale_store.get_all()
        self._apply_filters(update_message=False)

    def _current_keyword(self) -> str:
        return self._keyword_input.text().strip()

    def _current_status_filter(self) -> str:
        return self._status_filter.currentText().strip() or "全部"

    def _apply_filters(self, *_args, update_message: bool = True):
        self._visible_groups = filter_groups(
            self._all_groups,
            keyword=self._current_keyword(),
            status_filter=self._current_status_filter(),
        )
        self._render_group_table()
        self._render_group_details()
        self._update_status_label("" if not update_message else self._last_action_message)

    def _render_group_table(self):
        self._group_table.setRowCount(len(self._visible_groups))
        selected_row = -1
        for row, group in enumerate(self._visible_groups):
            pending_count = sum(1 for listing in group.listings if listing.delist_pending)
            values = [
                group.group_id,
                group.machine_code or "—",
                group.imei or "—",
                group.model or "—",
                str(len(group.listings)),
                str(pending_count),
                self._format_time(group.updated_at),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, group.group_id)
                self._group_table.setItem(row, column, item)
            if group.group_id == self._selected_group_id:
                selected_row = row

        if self._visible_groups and selected_row < 0:
            selected_row = 0
            self._selected_group_id = self._visible_groups[0].group_id
        if selected_row >= 0:
            self._group_table.blockSignals(True)
            self._group_table.selectRow(selected_row)
            self._group_table.blockSignals(False)
        else:
            self._selected_group_id = ""

    def _selected_group(self) -> SameSaleGroup | None:
        for group in self._visible_groups:
            if group.group_id == self._selected_group_id:
                return group
        return None

    def _render_group_details(self):
        group = self._selected_group()
        self._listing_table.setRowCount(0)
        if group is None:
            self._summary_label.setText("请选择左侧同售分组。")
            self._selected_listing_identity = None
            return

        sold_count = sum(1 for listing in group.listings if listing.sold)
        pending_count = sum(1 for listing in group.listings if listing.delist_pending)
        delisted_count = sum(1 for listing in group.listings if listing.delisted)
        machine_text = group.machine_code or "—"
        imei_text = group.imei or "—"
        model_parts = [group.model, group.condition, group.capacity, group.color]
        model_text = " ".join(part for part in model_parts if part).strip() or "—"
        self._summary_label.setText(
            "\n".join(
                [
                    f"分组ID：{group.group_id}",
                    f"质检码：{machine_text}    IMEI：{imei_text}",
                    f"型号信息：{model_text}",
                    f"备注：{group.note or '—'}",
                    f"统计：挂单 {len(group.listings)} / 已售 {sold_count} / 待下架 {pending_count} / 已下架 {delisted_count}",
                ]
            )
        )

        self._listing_table.setRowCount(len(group.listings))
        selected_row = -1
        for row, listing in enumerate(group.listings):
            identity = listing_identity(listing)
            values = [
                listing.platform_label or listing.platform or "—",
                listing.account_name or "—",
                listing.product_id or "—",
                listing.qc_code or "—",
                listing.imei or "—",
                listing.status or "—",
                f"¥{listing.price:,.0f}" if listing.price else "—",
                self._todo_text(listing),
                self._format_time(listing.updated_at),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, identity)
                self._apply_listing_row_style(item, listing)
                self._listing_table.setItem(row, column, item)
            if identity == self._selected_listing_identity:
                selected_row = row

        if group.listings and selected_row < 0:
            selected_row = 0
            self._selected_listing_identity = listing_identity(group.listings[0])
        if selected_row >= 0:
            self._listing_table.blockSignals(True)
            self._listing_table.selectRow(selected_row)
            self._listing_table.blockSignals(False)
        else:
            self._selected_listing_identity = None

    def _apply_listing_row_style(self, item: QTableWidgetItem, listing: SameSaleListing):
        if listing.sold:
            item.setBackground(QColor("#fce8e6"))
        elif listing.delist_pending:
            item.setBackground(QColor("#fff8e1"))
        elif listing.delisted:
            item.setBackground(QColor("#e8f5e9"))

    def _format_time(self, value: str) -> str:
        return str(value or "")[:16].replace("T", " ") or "—"

    def _todo_text(self, listing: SameSaleListing) -> str:
        if listing.delist_pending:
            return "待下架"
        if listing.delisted:
            return "已完成"
        return "—"

    def _on_group_selected(self):
        row = self._group_table.currentRow()
        if row < 0 or row >= len(self._visible_groups):
            return
        self._selected_group_id = self._visible_groups[row].group_id
        self._selected_listing_identity = None
        self._render_group_details()
        self._update_status_label(self._last_action_message)

    def _on_listing_selected(self):
        row = self._listing_table.currentRow()
        group = self._selected_group()
        if row < 0 or group is None or row >= len(group.listings):
            self._selected_listing_identity = None
            return
        self._selected_listing_identity = listing_identity(group.listings[row])
        self._update_status_label(self._last_action_message)

    def _selected_listing(self) -> SameSaleListing | None:
        return resolve_listing(self._selected_group(), self._selected_listing_identity)

    def _update_status_label(self, action_message: str = ""):
        total_groups = self.ctx.same_sale_store.count()
        visible_groups = len(self._visible_groups)
        sold_count = self.ctx.same_sale_store.sold_count()
        pending_count = self.ctx.same_sale_store.pending_count()
        base = f"分组 {visible_groups}/{total_groups} | 已售 {sold_count} | 待下架 {pending_count}"
        if action_message:
            base = f"{base} | {action_message}"
        self._status_label.setText(base)

    def _set_action_message(self, message: str):
        self._last_action_message = message
        self._update_status_label(message)

    def _require_selected_listing(self) -> tuple[SameSaleGroup | None, SameSaleListing | None]:
        group = self._selected_group()
        listing = self._selected_listing()
        if group is None or listing is None:
            self._set_action_message("未选择挂单")
            return None, None
        return group, listing

    def _mark_sold(self):
        group, listing = self._require_selected_listing()
        if group is None or listing is None:
            return
        confirmed = QMessageBox.question(
            self,
            "标记已售",
            f"确认把 {listing.platform_label or listing.platform} 挂单标记为已售，并把同组其他挂单转为待下架？",
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        ok = self.ctx.same_sale_store.mark_sold(
            group.group_id,
            listing.platform,
            product_id=listing.product_id,
            qc_code=listing.qc_code,
            imei=listing.imei,
        )
        self.refresh()
        self._set_action_message("已标记为已售" if ok else "标记已售失败")

    def _mark_delisted(self):
        group, listing = self._require_selected_listing()
        if group is None or listing is None:
            return
        ok = self.ctx.same_sale_store.mark_delisted(
            group.group_id,
            listing.platform,
            product_id=listing.product_id,
            qc_code=listing.qc_code,
            imei=listing.imei,
        )
        self.refresh()
        self._set_action_message("已标记为已下架" if ok else "标记已下架失败")
