# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QProgressBar,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from ..automation.tasks import (
    _pricing_preview,
    _refresh_imported_detail,
    _resolve_final_settle_price,
    _status_detail_from_obj,
    build_auto_preview_batch_rows,
    build_auto_preview_payload,
    match_imported_items_preview,
    task_erp_sync,
    task_refresh_imported_items_status,
    update_imported_items_category,
    update_imported_items_ignored,
    update_imported_items_selected,
)
from .manual_review_window import ManualReviewDialog
from ..services.zhuanzhuan_api import ImeiService
from ..config import STALE_STAGE2_DROP_PCT, cfg
from ..core.models import BatchItem, ProductStatus


class _AutoTabBridge(QObject):
    log = Signal(str)
    progress = Signal(dict)
    sync_finished = Signal(str)
    import_status_refreshed = Signal(dict, bool)
    store_changed = Signal()
    task_finished = Signal(str, object)


class ImportedProductsDialog(QDialog):
    COL_SELECTED = 0
    COL_ACCOUNT = 1
    COL_QC = 2
    COL_IMEI = 3
    COL_TITLE = 4
    COL_STATUS = 5
    COL_STATUS_DETAIL = 6
    COL_CATEGORY = 7
    COL_IGNORED = 8
    COL_CURRENT_PRICE = 9
    COL_COST_PRICE = 10
    COL_SUGGESTED_PRICE = 11
    COL_SETTLE_PRICE = 12
    COL_RESULT = 13
    STATUS_FILTER_OPTIONS = ["全部", "在售", "未上架", "已售", "已下架", "质检中", "未知"]

    def __init__(self, ctx, parent: QWidget | None = None):
        super().__init__(parent)
        self.ctx = ctx
        self._updating_table = False
        self.setWindowTitle("商品管理（ERP 导入）")
        self.resize(1500, 720)
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        hint = QLabel("当前窗口展示自动化实际处理范围：仅 imported_store 内商品。勾选=参与自动化；不处理区=保留但所有自动化跳过。")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        toolbar = QHBoxLayout()
        refresh_btn = QPushButton("刷新列表")
        copy_btn = QPushButton("复制选中")
        select_all_btn = QPushButton("全选处理")
        unselect_all_btn = QPushButton("取消全选")
        match_checked_btn = QPushButton("匹配建议价")
        preview_current_btn = QPushButton("当前行预览")
        category_btn = QPushButton("批量设分类")
        ignore_btn = QPushButton("移入不处理区")
        restore_btn = QPushButton("移出不处理区")
        remove_btn = QPushButton("移除选中")
        clear_btn = QPushButton("清空导入")
        for btn in (
            refresh_btn,
            copy_btn,
            select_all_btn,
            unselect_all_btn,
            match_checked_btn,
            preview_current_btn,
            category_btn,
            ignore_btn,
            restore_btn,
            remove_btn,
            clear_btn,
        ):
            toolbar.addWidget(btn)
        toolbar.addWidget(QLabel("状态:"))
        self._status_filter = QComboBox()
        self._status_filter.addItems(self.STATUS_FILTER_OPTIONS)
        toolbar.addWidget(self._status_filter)
        toolbar.addStretch(1)
        self._summary_label = QLabel("已导入 0 件")
        toolbar.addWidget(self._summary_label)
        layout.addLayout(toolbar)

        self._table = QTableWidget(0, 14)
        self._table.setHorizontalHeaderLabels([
            "处理",
            "账号",
            "质检码",
            "IMEI",
            "商品名",
            "状态",
            "状态明细",
            "分类",
            "不处理区",
            "当前价",
            "成本价",
            "建议价",
            "预计结算价",
            "处理结果",
        ])
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked
            | QTableWidget.EditTrigger.SelectedClicked
            | QTableWidget.EditTrigger.EditKeyPressed
        )
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._table, 1)

        preview_hint = QLabel("单击表格中的 1 行后，可点“当前行预览”刷新该商品建议价；“匹配建议价”只处理勾选商品。两者都只更新建议价/预览，不会直接改价或上架。")
        preview_hint.setWordWrap(True)
        layout.addWidget(preview_hint)

        self._preview_text = QPlainTextEdit()
        self._preview_text.setReadOnly(True)
        self._preview_text.setMinimumHeight(180)
        self._preview_text.setMaximumHeight(280)
        layout.addWidget(self._preview_text)

        refresh_btn.clicked.connect(self.refresh)
        copy_btn.clicked.connect(self._copy_selected)
        select_all_btn.clicked.connect(lambda: self._set_selected_for_rows(True))
        unselect_all_btn.clicked.connect(lambda: self._set_selected_for_rows(False))
        match_checked_btn.clicked.connect(self._match_checked_items)
        preview_current_btn.clicked.connect(self._match_current_item)
        category_btn.clicked.connect(self._batch_set_category)
        ignore_btn.clicked.connect(lambda: self._set_ignored_for_rows(True))
        restore_btn.clicked.connect(lambda: self._set_ignored_for_rows(False))
        remove_btn.clicked.connect(self._remove_selected)
        clear_btn.clicked.connect(self._clear_all)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.itemSelectionChanged.connect(self._update_preview)
        self._status_filter.currentIndexChanged.connect(self.refresh)

    def _imported_store(self):
        if hasattr(self.ctx, "zhuanzhuan") and hasattr(self.ctx.zhuanzhuan, "imported_store"):
            return self.ctx.zhuanzhuan.imported_store
        return getattr(self.ctx, "imported_store")

    def _emit_store_changed(self):
        parent = self.parentWidget()
        bridge = getattr(parent, "_bridge", None)
        if bridge is not None and hasattr(bridge, "store_changed"):
            bridge.store_changed.emit()
            return
        if parent is not None and hasattr(parent, "_handle_store_changed"):
            parent._handle_store_changed()

    def _row_product_id(self, row: int) -> str:
        cell = self._table.item(row, self.COL_QC)
        if cell is None:
            return ""
        return str(cell.data(Qt.ItemDataRole.UserRole) or "")

    def _selected_product_ids(self) -> list[str]:
        rows = sorted({index.row() for index in self._table.selectionModel().selectedRows()})
        return [pid for pid in (self._row_product_id(row) for row in rows) if pid]

    def _all_visible_product_ids(self) -> list[str]:
        ids: list[str] = []
        for row in range(self._table.rowCount()):
            pid = self._row_product_id(row)
            if pid:
                ids.append(pid)
        return ids

    def _set_check_item(self, row: int, col: int, checked: bool):
        item = self._table.item(row, col)
        if item is None:
            item = QTableWidgetItem("")
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            self._table.setItem(row, col, item)
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def _matches_status_filter(self, item) -> bool:
        selected = self._status_filter.currentText().strip() or "全部"
        if selected == "全部":
            return True
        status = getattr(item, "status", None)
        label = str(getattr(status, "label", "") or "").strip()
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
            "": "未知",
        }
        return mapping.get(raw, "未知") == selected

    def _filtered_items(self, items):
        return [item for item in items if self._matches_status_filter(item)]

    def refresh(self):
        store = self._imported_store()
        all_items = list(store.get_all())
        items = self._filtered_items(all_items)
        self._updating_table = True
        try:
            self._table.setRowCount(len(items))
            for row, item in enumerate(items):
                self._set_check_item(row, self.COL_SELECTED, getattr(item, "selected", True))

                account_item = QTableWidgetItem(item.account_name or "—")
                account_item.setFlags(account_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row, self.COL_ACCOUNT, account_item)

                qc_item = QTableWidgetItem(item.qc_code or "—")
                qc_item.setData(Qt.ItemDataRole.UserRole, item.product_id)
                qc_item.setFlags(qc_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row, self.COL_QC, qc_item)

                imei_item = QTableWidgetItem(getattr(item, "imei", "") or "—")
                imei_item.setFlags(imei_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row, self.COL_IMEI, imei_item)

                title_item = QTableWidgetItem(item.title or item.model or "—")
                title_item.setFlags(title_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row, self.COL_TITLE, title_item)

                status_label = getattr(getattr(item, "status", None), "label", "") or str(getattr(getattr(item, "status", None), "value", getattr(item, "status", "")) or "—")
                status_item = QTableWidgetItem(status_label)
                status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row, self.COL_STATUS, status_item)

                detail_item = QTableWidgetItem(getattr(item, "status_detail", "") or "—")
                detail_item.setFlags(detail_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row, self.COL_STATUS_DETAIL, detail_item)

                category_item = QTableWidgetItem(getattr(item, "category", "") or "")
                self._table.setItem(row, self.COL_CATEGORY, category_item)

                self._set_check_item(row, self.COL_IGNORED, getattr(item, "ignored", False))

                current_item = QTableWidgetItem(f"¥{float(item.current_price or 0):,.0f}")
                current_item.setFlags(current_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row, self.COL_CURRENT_PRICE, current_item)

                cost = getattr(item, "cost_price", None)
                cost_item = QTableWidgetItem(f"¥{float(cost):,.2f}" if cost is not None and float(cost) > 0 else "—")
                cost_item.setFlags(cost_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row, self.COL_COST_PRICE, cost_item)

                suggested = getattr(item, "suggested_price", None)
                suggested_item = QTableWidgetItem(f"¥{float(suggested):,.0f}" if suggested is not None else "—")
                suggested_item.setFlags(suggested_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row, self.COL_SUGGESTED_PRICE, suggested_item)

                settle = getattr(item, "suggested_settle_price", None)
                if settle is None:
                    settle = getattr(item, "settle_price", None)
                settle_item = QTableWidgetItem(f"¥{float(settle):,.0f}" if settle is not None else "—")
                settle_item.setFlags(settle_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row, self.COL_SETTLE_PRICE, settle_item)

                result_text = getattr(item, "op_message", "") or getattr(item, "reprice_msg", "") or "—"
                result_item = QTableWidgetItem(result_text)
                result_item.setFlags(result_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row, self.COL_RESULT, result_item)
        finally:
            self._updating_table = False

        selected_count = sum(1 for it in all_items if getattr(it, "selected", True))
        selected_visible_count = sum(1 for it in items if getattr(it, "selected", True))
        self._summary_label.setText(f"已导入 {len(all_items)} 件｜当前显示 {len(items)} 件｜勾选 {selected_count} 件（显示中 {selected_visible_count} 件）")
        self._update_preview()

    def _copy_selected(self):
        ids = self._selected_product_ids()
        if not ids:
            return
        store = self._imported_store()
        lines: list[str] = []
        for pid in ids:
            item = store.get(pid)
            if item is None:
                continue
            lines.append("\t".join([
                item.account_name or "",
                item.qc_code or "",
                getattr(item, "imei", "") or "",
                item.title or item.model or "",
                str(getattr(item, "status", "") or ""),
                str(item.current_price or ""),
                str(getattr(item, "suggested_price", "") or ""),
            ]))
        QApplication.clipboard().setText("\n".join(lines))

    def _set_selected_for_rows(self, checked: bool):
        ids = self._all_visible_product_ids()
        if not ids:
            return
        store = self._imported_store()
        update_imported_items_selected(store, ids, checked)
        self.refresh()
        self._emit_store_changed()

    def _match_items(self, ids: list[str]):
        if not ids:
            return
        store = self._imported_store()
        result = match_imported_items_preview(
            store,
            ids,
            self.ctx.sold_cache,
            self.ctx.rule_engine,
        )
        matched = int(result.get("matched") or 0)
        skipped = int(result.get("skipped") or 0)
        QMessageBox.information(self, "提示", f"匹配完成：成功 {matched}，跳过 {skipped}")
        self.refresh()
        self._emit_store_changed()

    def _match_checked_items(self):
        store = self._imported_store()
        ids = [
            item.product_id
            for item in store.get_all()
            if getattr(item, "selected", True) and not getattr(item, "ignored", False)
        ]
        self._match_items(ids)

    def _match_current_item(self):
        ids = self._selected_product_ids()
        if not ids:
            QMessageBox.information(self, "提示", "请先选中 1 行商品")
            return
        self._match_items([ids[0]])

    def _batch_set_category(self):
        ids = self._selected_product_ids()
        if not ids:
            QMessageBox.information(self, "提示", "请先选择要设置分类的行")
            return
        text, ok = QInputDialog.getText(self, "批量设分类", "请输入分类:")
        if not ok:
            return
        store = self._imported_store()
        update_imported_items_category(store, ids, text or "")
        self.refresh()
        self._emit_store_changed()

    def _set_ignored_for_rows(self, ignored: bool):
        ids = self._selected_product_ids() or self._all_visible_product_ids()
        if not ids:
            return
        store = self._imported_store()
        update_imported_items_ignored(store, ids, ignored)
        self.refresh()
        self._emit_store_changed()

    def _remove_selected(self):
        ids = self._selected_product_ids()
        if not ids:
            QMessageBox.information(self, "提示", "请先选中要移除的行")
            return
        if QMessageBox.question(self, "确认", f"确认移除 {len(ids)} 件商品？") != QMessageBox.StandardButton.Yes:
            return
        store = self._imported_store()
        for pid in ids:
            store.remove(pid)
        self.refresh()
        self._emit_store_changed()

    def _clear_all(self):
        store = self._imported_store()
        total = store.count()
        if total <= 0:
            return
        if QMessageBox.question(self, "确认", f"确认清空全部 {total} 件导入商品？") != QMessageBox.StandardButton.Yes:
            return
        store.clear()
        self.refresh()
        self._emit_store_changed()

    def _on_item_changed(self, cell: QTableWidgetItem):
        if self._updating_table or cell is None:
            return
        row = cell.row()
        pid = self._row_product_id(row)
        if not pid:
            return
        store = self._imported_store()
        col = cell.column()
        if col == self.COL_SELECTED:
            store.update_item(pid, selected=cell.checkState() == Qt.CheckState.Checked)
        elif col == self.COL_IGNORED:
            store.update_item(pid, ignored=cell.checkState() == Qt.CheckState.Checked)
        elif col == self.COL_CATEGORY:
            store.update_item(pid, category=(cell.text() or "").strip())
        else:
            return
        self._emit_store_changed()

    def _update_preview(self):
        ids = self._selected_product_ids()
        if not ids:
            self._preview_text.setPlainText("单击表格中的 1 行后，可点“当前行预览”刷新该商品建议价；“匹配建议价”只处理勾选商品。")
            return
        item = self._imported_store().get(ids[0])
        if item is None:
            self._preview_text.setPlainText("未找到当前选中商品")
            return
        status_label = getattr(getattr(item, "status", None), "label", "") or str(getattr(getattr(item, "status", None), "value", getattr(item, "status", "")) or "—")
        preview_text = _pricing_preview(getattr(item, "pricing", None), self.ctx.rule_engine, item) if getattr(item, "pricing", None) is not None else "暂无定价结果，请点“当前行预览”或“匹配建议价”。"
        lines = [
            f"账号: {item.account_name or '—'}",
            f"质检码: {item.qc_code or '—'}",
            f"IMEI: {getattr(item, 'imei', '') or '—'}",
            f"商品: {item.title or item.model or '—'}",
            f"状态: {status_label}",
            f"当前价: ¥{float(item.current_price or 0):,.0f}",
            f"建议价: {'¥%s' % format(item.suggested_price, ',.0f') if item.suggested_price is not None else '—'}",
            f"建议结算价: {'¥%s' % format(item.suggested_settle_price, ',.0f') if item.suggested_settle_price is not None else '—'}",
            f"处理状态: {item.op_status or '待处理'}",
            f"处理结果: {item.op_message or item.reprice_msg or '—'}",
            "",
            "定价预览:",
            preview_text,
        ]
        self._preview_text.setPlainText("\n".join(lines))




class ZhuanzhuanAutoQtTab(QWidget):
    TASKS = [
        ("auto_reprice", "模块1：自动调价（仅导入商品）", "auto_reprice_interval", 120),
        ("stale_drop", "模块2：滞销降价（仅导入商品）", "auto_reprice_interval", 120),
        ("auto_list", "模块3：未上架自动上架（仅导入商品）", "auto_list_interval", 120),
        ("sales_report", "模块4：导入状态播报（仅导入商品）", "sales_report_interval", 60),
    ]
    PREVIEW_BATCH_LIMIT = 20
    IMPORT_STATUS_REFRESH_INTERVAL_MS = 60 * 1000
    IMPORT_STATUS_REFRESH_LIMIT = 0
    IMPORT_STATUS_PRIORITY_LOG = 10
    IMPORT_STATUS_PRIORITY_IDLE = 20
    IMPORT_STATUS_PRIORITY_REFRESH_RUNNING = 40
    IMPORT_STATUS_PRIORITY_REFRESH_SUMMARY = 50
    IMPORT_STATUS_PRIORITY_IMPORT_START = 60
    IMPORT_STATUS_PRIORITY_IMPORT_PROGRESS = 70
    IMPORT_STATUS_PRIORITY_IMPORT_DONE = 80

    def __init__(self, app_ctx):
        super().__init__()
        self.ctx = app_ctx
        self.platform_ctx = app_ctx.zhuanzhuan
        self._bridge = _AutoTabBridge(self)
        self._erp_sync_running = False
        self._refreshing_import_status = False
        self._import_status_refresh_running = False
        self._last_import_summary = ""
        self._last_refresh_trigger = ""
        self._last_refresh_time = ""
        self._last_refresh_summary = ""
        self._pending_refresh_report: dict | None = None
        self._import_status_text_priority = self.IMPORT_STATUS_PRIORITY_IDLE
        self._import_status_text_source = "idle"
        self._manage_dialog: ImportedProductsDialog | None = None
        self._manual_review_dialog: ManualReviewDialog | None = None
        self._task_enabled: dict[str, QCheckBox] = {}
        self._task_interval: dict[str, QSpinBox] = {}
        self._stale_stage2_drop_pct: QSpinBox | None = None
        self._bridge.log.connect(self._log)
        self._bridge.progress.connect(self._handle_import_progress)
        self._bridge.sync_finished.connect(self._after_sync_erp_import)
        self._bridge.import_status_refreshed.connect(self._after_import_status_refreshed)
        self._bridge.store_changed.connect(self._handle_store_changed)
        self._bridge.task_finished.connect(self._on_task_finished)
        self._build_ui()
        self.ctx.automation_log = self._bridge.log.emit
        self.ctx.automation_task_finished = self._bridge.task_finished.emit
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start()
        self._import_status_timer = QTimer(self)
        self._import_status_timer.setInterval(self.IMPORT_STATUS_REFRESH_INTERVAL_MS)
        self._import_status_timer.timeout.connect(self._trigger_auto_import_status_refresh)
        self._import_status_timer.start()
        self._refresh_import_status()
        self._update_manual_review_button()
        self._refresh_preview_all()
        self._refresh_status()


    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        root.addWidget(scroll, 1)

        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)

        intro = QLabel("自动化已并入转转模块：ERP 导入、任务调度、调价预览、企业微信配置都在这里。")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(self._build_manual_review_box())
        layout.addWidget(self._build_erp_box())
        layout.addWidget(self._build_import_box())
        layout.addWidget(self._build_scheduler_box())
        layout.addWidget(self._build_tasks_box())
        layout.addWidget(self._build_explain_box())
        layout.addWidget(self._build_log_box())
        layout.addWidget(self._build_preview_box())
        layout.addWidget(self._build_wx_box())

        self._status_label = QLabel()
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        layout.addStretch(1)

    def _build_manual_review_box(self):
        box = QGroupBox("手动确认调价")
        layout = QVBoxLayout(box)
        row = QHBoxLayout()
        self._manual_review_btn = QPushButton("手动确认调价（待确认 0）")
        row.addWidget(self._manual_review_btn)
        row.addStretch(1)
        layout.addLayout(row)
        tip = QLabel("统一处理所有调价人工确认项：接受调价 / 拒绝（永久） / 拒绝并移入不处理区。")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        self._manual_review_btn.clicked.connect(self._open_manual_review_window)
        return box

    def _build_erp_box(self):
        box = QGroupBox("ERP 配置")
        form = QFormLayout(box)
        self._erp_token = QLineEdit(getattr(self.ctx.erp_config, "token", "") or "")
        self._erp_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._erp_version = QLineEdit(getattr(self.ctx.erp_config, "version", "") or "")
        form.addRow("Authorization Token", self._erp_token)
        form.addRow("Version", self._erp_version)
        btn_row = QHBoxLayout()
        save_btn = QPushButton("保存 ERP 配置")
        btn_row.addWidget(save_btn)
        btn_row.addStretch(1)
        form.addRow(btn_row)
        form.addRow(QLabel("请填写旧版爱管机 Authorization Token；Version 可留空，系统会按当天自动补默认值。"))
        save_btn.clicked.connect(self._save_erp_config)
        return box

    def _build_import_box(self):
        box = QGroupBox("导入商品管理")
        layout = QVBoxLayout(box)
        toolbar = QHBoxLayout()
        self._sync_btn = QPushButton("从 ERP 同步并导入")
        self._manage_btn = QPushButton("打开商品管理")
        self._refresh_import_btn = QPushButton("刷新导入状态")
        preview_btn = QPushButton("刷新调价预览")
        toolbar.addWidget(self._sync_btn)
        toolbar.addWidget(self._manage_btn)
        toolbar.addWidget(self._refresh_import_btn)
        toolbar.addWidget(preview_btn)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)
        self._import_status_label = QLabel("商品管理范围：未导入商品")
        self._import_status_label.setWordWrap(True)
        layout.addWidget(self._import_status_label)
        self._import_progress_label = QLabel("导入状态：空闲")
        self._import_progress_label.setWordWrap(True)
        layout.addWidget(self._import_progress_label)
        self._import_progress = QProgressBar()
        self._import_progress.setRange(0, 1)
        self._import_progress.setValue(0)
        self._import_progress.setTextVisible(False)
        layout.addWidget(self._import_progress)
        tip = QLabel("自动调价、滞销降价、未上架自动上架和商品管理窗口都只会处理这里导入的商品。导入时可在这里查看状态，详细过程会写入下方运行日志。")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        self._sync_btn.clicked.connect(self._sync_erp_import)
        self._manage_btn.clicked.connect(self._open_imported_window)
        self._refresh_import_btn.clicked.connect(self._refresh_import_status_clicked)
        preview_btn.clicked.connect(self._refresh_preview_all)
        return box

    def _build_scheduler_box(self):
        box = QGroupBox("调度器")
        layout = QHBoxLayout(box)
        self._scheduler_running = QCheckBox("调度器运行中")
        refresh_btn = QPushButton("刷新状态")
        layout.addWidget(self._scheduler_running)
        layout.addWidget(refresh_btn)
        layout.addStretch(1)
        self._scheduler_running.toggled.connect(self._toggle_scheduler)
        refresh_btn.clicked.connect(self._refresh_status)
        return box

    def _build_tasks_box(self):
        box = QGroupBox("自动化模块")
        layout = QGridLayout(box)
        for row, (task_name, label, interval_key, fallback) in enumerate(self.TASKS):
            enabled = QCheckBox(label)
            interval = QSpinBox()
            interval.setRange(1, 1440)
            interval.setValue(int(self.ctx.cfg_val(interval_key, fallback)))
            apply_btn = QPushButton("应用")
            run_btn = QPushButton("立即执行")
            self._task_enabled[task_name] = enabled
            self._task_interval[task_name] = interval
            layout.addWidget(enabled, row, 0)
            layout.addWidget(QLabel("间隔(分钟):"), row, 1)
            layout.addWidget(interval, row, 2)
            layout.addWidget(apply_btn, row, 3)
            layout.addWidget(run_btn, row, 4)
            enabled.toggled.connect(lambda checked, name=task_name: self._toggle_task_enabled(name, checked))
            apply_btn.clicked.connect(lambda _=False, name=task_name, key=interval_key, field=interval: self._apply_interval(name, key, field.value()))
            run_btn.clicked.connect(lambda _=False, name=task_name: self._run_now(name))

        stale_row = len(self.TASKS)
        stale_label = QLabel("阶段2额外降价比例(%):")
        stale_pct = QSpinBox()
        stale_pct.setRange(1, 99)
        stale_pct.setValue(int(self.ctx.cfg_val("stale_stage2_drop_pct", STALE_STAGE2_DROP_PCT)))
        stale_apply_btn = QPushButton("应用")
        self._stale_stage2_drop_pct = stale_pct
        layout.addWidget(stale_label, stale_row, 1)
        layout.addWidget(stale_pct, stale_row, 2)
        layout.addWidget(stale_apply_btn, stale_row, 3)
        stale_apply_btn.clicked.connect(self._apply_stale_stage2_drop_pct)
        return box

    def _build_explain_box(self):
        box = QGroupBox("模块说明")
        layout = QVBoxLayout(box)
        label = QLabel(
            "• 模块1 自动调价：只处理导入商品管理中的商品，先走系统建议价与启用规则，再叠加价格微调，最终执行改价。\n"
            "• 模块2 滞销降价：只处理导入商品管理中在售的商品，按滞销阶段决定是否降到保守价或继续按比例下调。\n"
            "• 模块3 未上架自动上架：只处理导入商品管理中符合资格且当前未上架的商品。\n"
            "• 模块4 导入状态播报：仅播报导入商品的状态变更（如已售、质检中→未上架），不直接修改商品价格或状态。"
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        return box

    def _build_preview_box(self):
        box = QGroupBox("下次系统调价预览（价格微调可选）")
        layout = QVBoxLayout(box)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("价格微调方式:"))
        self._custom_offset_mode = QComboBox()
        self._custom_offset_mode.addItems(["off", "fixed", "percent"])
        self._custom_offset_mode.setCurrentText(self._normalize_offset_mode(self.ctx.cfg_val("auto_reprice_custom_offset_mode", "off")))
        toolbar.addWidget(self._custom_offset_mode)
        toolbar.addWidget(QLabel("微调数值:"))
        self._custom_offset_value = QLineEdit(str(self._safe_float(self.ctx.cfg_val("auto_reprice_custom_offset_value", 0.0))))
        self._custom_offset_value.setMaximumWidth(120)
        toolbar.addWidget(self._custom_offset_value)
        save_btn = QPushButton("应用微调设置")
        reset_btn = QPushButton("恢复系统建议价")
        toolbar.addWidget(save_btn)
        toolbar.addWidget(reset_btn)
        toolbar.addWidget(QLabel("价格微调仅用于微调建议价：off=关闭；fixed=固定加减金额；percent=按系统建议价百分比加减。所有实际改价仍以手动确认窗口为统一入口。"))
        toolbar.addStretch(1)
        layout.addLayout(toolbar)
        self._preview_text = QPlainTextEdit()
        self._preview_text.setReadOnly(True)
        self._preview_text.setMinimumHeight(160)
        self._preview_text.setMaximumHeight(240)
        layout.addWidget(self._preview_text, 1)

        self._preview_table = QTableWidget(0, 10)
        self._preview_table.setHorizontalHeaderLabels([
            "商品",
            "账号",
            "当前价",
            "系统建议",
            "最终执行",
            "差价",
            "人工确认",
            "确认原因",
            "命中规则",
            "摘要",
        ])
        self._preview_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._preview_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._preview_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._preview_table.setAlternatingRowColors(True)
        self._preview_table.verticalHeader().setVisible(False)
        self._preview_table.horizontalHeader().setStretchLastSection(True)
        self._preview_table.setMinimumHeight(220)
        self._preview_table.setMaximumHeight(360)
        layout.addWidget(self._preview_table)
        save_btn.clicked.connect(self._save_custom_offset)
        reset_btn.clicked.connect(self._reset_custom_offset)
        return box

    def _build_wx_box(self):
        box = QGroupBox("企业微信配置")
        form = QFormLayout(box)
        self._wx_fields = {
            "corp_id": QLineEdit(self.ctx.wxapp_config.get("corp_id", "")),
            "corp_secret": QLineEdit(self.ctx.wxapp_config.get("corp_secret", "")),
            "agent_id": QLineEdit(self.ctx.wxapp_config.get("agent_id", "")),
            "robot_webhook": QLineEdit(self.ctx.wxapp_config.get("robot_webhook", "")),
        }
        self._wx_fields["corp_secret"].setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("企业ID", self._wx_fields["corp_id"])
        form.addRow("应用Secret", self._wx_fields["corp_secret"])
        form.addRow("AgentID", self._wx_fields["agent_id"])
        form.addRow("机器人Webhook", self._wx_fields["robot_webhook"])
        row = QHBoxLayout()
        save_btn = QPushButton("保存配置")
        row.addWidget(save_btn)
        row.addStretch(1)
        form.addRow(row)
        save_btn.clicked.connect(self._save_wx_config)
        return box

    def _build_log_box(self):
        box = QGroupBox("运行日志")
        layout = QVBoxLayout(box)
        self._log_text = QPlainTextEdit()
        self._log_text.setReadOnly(True)
        self._log_text.setMinimumHeight(220)
        layout.addWidget(self._log_text, 1)
        row = QHBoxLayout()
        clear_btn = QPushButton("清空日志")
        row.addStretch(1)
        row.addWidget(clear_btn)
        layout.addLayout(row)
        clear_btn.clicked.connect(self._clear_log)
        return box

    def _normalize_offset_mode(self, value) -> str:
        mode = str(value or "off").strip().lower()
        return mode if mode in {"off", "fixed", "percent"} else "off"

    def _safe_float(self, value, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    def _preview_source_item(self):
        items = [
            item for item in self.platform_ctx.imported_store.get_all()
            if not getattr(item, "ignored", False)
        ]
        if not items:
            return None
        checked = [item for item in items if getattr(item, "selected", True)]
        return checked[0] if checked else items[0]

    def _refresh_preview(self):
        item = self._preview_source_item()
        if item is None:
            self._preview_text.setPlainText(
                "暂无导入商品，无法预览自动调价。\n\n"
                "先执行“从 ERP 同步并导入”后，再回来查看预览。\n\n"
                "说明：\n"
                "- 自动调价 / 滞销降价 / 未上架自动上架都只处理导入商品\n"
                "- 预览不会扫描全店，只从当前导入商品中取样"
            )
            return

        mode = self._normalize_offset_mode(self._custom_offset_mode.currentText())
        value = self._safe_float(self._custom_offset_value.text(), 0.0)
        payload = build_auto_preview_payload(
            item,
            self.ctx.sold_cache,
            self.ctx.rule_engine,
            custom_mode=mode,
            custom_value=value,
        )
        pipeline = payload.get("pipeline") or {}
        decision = pipeline.get("decision") or {}
        preview = pipeline.get("preview") or "—"
        explain_lines = decision.get("explain_lines") or ["无启用规则"]
        final_price = pipeline.get("suggested_price")
        system_price = decision.get("system_price")
        final_settle = pipeline.get("suggested_settled_price") or pipeline.get("suggested_settle_price")
        diff_text = "—"
        if final_price is not None:
            diff_text = f"{final_price - item.current_price:+.0f}"
        official_reference_price = decision.get("official_reference_price")
        official_deviation_pct = decision.get("official_deviation_pct")
        official_risk_triggered = bool(decision.get("official_risk_triggered"))
        official_grade_name = str(decision.get("official_reference_grade_name") or "")
        official_reference_text = "—"
        if official_reference_price is not None:
            grade_suffix = f"（{official_grade_name}）" if official_grade_name else ""
            official_reference_text = f"¥{format(official_reference_price, ',.0f')}{grade_suffix}"
        official_deviation_text = "—"
        if official_deviation_pct is not None:
            official_deviation_text = f"{official_deviation_pct:.2f}%"
            if official_risk_triggered:
                official_deviation_text += "（超阈值，待确认）"
        module_preview = payload.get("module_preview") or {}
        lines = [
            "处理范围：",
            "- 自动调价：仅导入商品",
            "- 滞销降价：仅导入商品",
            "- 未上架自动上架：仅导入商品中符合资格且当前未上架的商品",
            "",
            f"预览样本：{item.qc_code or item.product_id} / {item.title or item.model or '—'}",
            f"账号：{item.account_name or '—'}",
            f"当前价：¥{item.current_price:,.0f}",
            f"系统建议价：{'¥%s' % format(system_price, ',.0f') if system_price is not None else '—'}",
            f"价格微调：{decision.get('custom_summary', '关闭')}",
            f"最终执行价：{'¥%s' % format(final_price, ',.0f') if final_price is not None else '—'}",
            f"建议调价：{diff_text if final_price is not None else '—'} 元",
            f"本次最终会从 {'¥%s' % format(item.current_price, ',.0f')} 改到 {'¥%s' % format(final_price, ',.0f') if final_price is not None else '—'}",
            f"预计结算价：{'¥%s' % format(final_settle, ',.0f') if final_settle is not None else '—'}",
            f"官方参考价：{official_reference_text}",
            f"相对官方偏离：{official_deviation_text}",
            f"相对当前差价：{diff_text}",
            f"命中规则：{decision.get('rule_hit') or '未命中，使用基础建议价'}",
            "",
            "下次系统运行预览：",
            f"- 自动调价：{module_preview.get('auto_reprice', '-')}",
            f"- 滞销降价：{module_preview.get('stale_drop', '-')}",
            f"- 未上架自动上架：{module_preview.get('auto_list', '-')}",
            "",
            "规则说明：",
            *explain_lines,
            "",
            "摘要：",
            preview,
            "",
            "提示：当前预览按上方价格微调设置即时计算；点击“应用微调设置”后，自动调价任务会按相同口径执行。",
        ]
        self._preview_text.setPlainText("\n".join(lines))

    def _set_preview_cell(self, row: int, col: int, text: str):
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._preview_table.setItem(row, col, item)

    def _set_import_status_text(self, text: str, *, priority: int, source: str, force: bool = False):
        if not force and priority < self._import_status_text_priority:
            return
        self._import_status_text_priority = priority
        self._import_status_text_source = source
        self._import_progress_label.setText(text)

    def _reset_import_status_priority(self):
        if self._erp_sync_running:
            self._import_status_text_priority = self.IMPORT_STATUS_PRIORITY_IMPORT_PROGRESS
            self._import_status_text_source = "import"
            return
        if self._import_status_refresh_running:
            self._import_status_text_priority = self.IMPORT_STATUS_PRIORITY_REFRESH_RUNNING
            self._import_status_text_source = "refresh"
            return
        self._import_status_text_priority = self.IMPORT_STATUS_PRIORITY_IDLE
        self._import_status_text_source = "idle"

    def _handle_import_progress(self, event: dict):
        if not isinstance(event, dict):
            return
        stage = str(event.get("stage") or "").strip()
        current = max(int(event.get("current") or 0), 0)
        total = max(int(event.get("total") or 0), 0)
        message = str(event.get("message") or "").strip()

        if total > 0:
            self._import_progress.setRange(0, total)
            self._import_progress.setValue(min(current, total))
            self._import_progress.setTextVisible(True)
        else:
            self._import_progress.setRange(0, 0)
            self._import_progress.setTextVisible(False)

        stage_label_map = {
            "fetch": "拉取",
            "match": "匹配",
            "write": "写入",
            "done": "完成",
        }
        stage_label = stage_label_map.get(stage, stage or "处理中")
        progress_text = f"{current}/{total}" if total > 0 else "进行中"
        if message:
            self._set_import_status_text(
                f"导入状态：[{stage_label}] {progress_text}｜{message}",
                priority=self.IMPORT_STATUS_PRIORITY_IMPORT_PROGRESS,
                source="import_progress",
            )
        else:
            self._set_import_status_text(
                f"导入状态：[{stage_label}] {progress_text}",
                priority=self.IMPORT_STATUS_PRIORITY_IMPORT_PROGRESS,
                source="import_progress",
            )

    def _refresh_preview_batch(self):
        all_items = [
            item for item in self.platform_ctx.imported_store.get_all()
            if not getattr(item, "ignored", False)
        ]
        candidates = [item for item in all_items if getattr(item, "selected", True)]
        source_items = candidates if candidates else all_items
        limit = self.PREVIEW_BATCH_LIMIT
        preview_items = source_items[:limit]

        self._preview_table.setRowCount(len(preview_items))
        if not preview_items:
            self._preview_table.setToolTip("暂无可预览商品。")
            return

        mode = self._normalize_offset_mode(self._custom_offset_mode.currentText())
        value = self._safe_float(self._custom_offset_value.text(), 0.0)
        self._preview_table.setToolTip(f"显示 {len(preview_items)} / {len(source_items)} 条（优先勾选商品）")

        rows = build_auto_preview_batch_rows(
            preview_items,
            self.ctx.sold_cache,
            self.ctx.rule_engine,
            custom_mode=mode,
            custom_value=value,
            limit=limit,
        )

        for row, row_data in enumerate(rows):
            item = row_data.get("item")
            pipeline = row_data.get("pipeline") or {}
            decision = row_data.get("decision") or {}
            current_price = row_data.get("current_price")
            system_price = row_data.get("system_price")
            final_price = row_data.get("final_price")
            delta = row_data.get("delta")
            delta_text = f"{delta:+.0f}" if delta is not None else "—"

            self._set_preview_cell(row, 0, (item.qc_code or item.product_id or "—"))
            self._set_preview_cell(row, 1, (item.account_name or "—"))
            self._set_preview_cell(row, 2, f"¥{float(current_price or 0):,.0f}")
            self._set_preview_cell(row, 3, f"¥{float(system_price):,.0f}" if system_price is not None else "—")
            self._set_preview_cell(row, 4, f"¥{float(final_price):,.0f}" if final_price is not None else "—")
            self._set_preview_cell(row, 5, delta_text)
            self._set_preview_cell(row, 6, "是" if decision.get("needs_manual_review") else "否")
            self._set_preview_cell(row, 7, decision.get("manual_review_reason") or "—")
            self._set_preview_cell(row, 8, decision.get("rule_hit") or "未命中")
            self._set_preview_cell(row, 9, pipeline.get("preview") or "—")

    def _refresh_preview_all(self):
        self._refresh_preview()
        self._refresh_preview_batch()

    def _set_import_running(self, running: bool):
        self._erp_sync_running = running
        self._sync_btn.setEnabled(not running)
        self._manage_btn.setEnabled(not running)
        if running:
            self._import_progress.setRange(0, 0)
            self._set_import_status_text(
                "导入状态：正在从 ERP 同步并导入，详细过程见下方运行日志…",
                priority=self.IMPORT_STATUS_PRIORITY_IMPORT_START,
                source="import_start",
                force=True,
            )
        else:
            self._import_progress.setRange(0, 1)
            self._import_progress.setValue(0)
            self._reset_import_status_priority()
            self._set_import_status_text(
                "导入状态：空闲",
                priority=self.IMPORT_STATUS_PRIORITY_IDLE,
                source="idle",
                force=True,
            )

    def _refresh_import_status_clicked(self):
        self._start_import_status_refresh(manual=True)

    def _start_import_status_refresh(self, *, manual: bool):
        if self._erp_sync_running or self._import_status_refresh_running:
            return
        self._import_status_refresh_running = True
        self._refreshing_import_status = manual
        self._reset_import_status_priority()
        trigger = "manual" if manual else "auto"
        if manual:
            self._refresh_import_btn.setEnabled(False)
        self._set_import_status_text(
            "导入状态：正在刷新导入商品实时状态…",
            priority=self.IMPORT_STATUS_PRIORITY_REFRESH_RUNNING,
            source="refresh_running",
            force=True,
        )
        threading.Thread(target=self._do_refresh_import_status, args=(trigger,), daemon=True).start()

    def _trigger_auto_import_status_refresh(self):
        self._start_import_status_refresh(manual=False)

    def _do_refresh_import_status(self, trigger: str):
        success = True
        report: dict = {
            "trigger": trigger,
            "summary": "",
        }
        try:
            result = task_refresh_imported_items_status(
                self.ctx.account_store,
                self.platform_ctx.imported_store,
                on_progress=self._bridge.log.emit,
                limit=self.IMPORT_STATUS_REFRESH_LIMIT,
            )
            if isinstance(result, dict):
                report.update(result)
                report["summary"] = str(result.get("summary", "实时刷新完成"))
            else:
                report["summary"] = str(result)
        except Exception as exc:
            success = False
            summary = f"实时刷新失败: {exc}"
            report.update(
                {
                    "summary": summary,
                    "error": str(exc),
                    "fail": 1,
                }
            )
            self._bridge.log.emit(summary)
        self._bridge.import_status_refreshed.emit(report, success)

    def _after_import_status_refreshed(self, report: dict, success: bool):
        was_manual = self._refreshing_import_status
        self._refreshing_import_status = False
        self._import_status_refresh_running = False
        if was_manual:
            self._refresh_import_btn.setEnabled(True)

        data = report if isinstance(report, dict) else {"summary": str(report or "")}
        summary = str(data.get("summary") or "实时刷新完成")
        trigger = str(data.get("trigger") or ("manual" if was_manual else "auto"))
        trigger_label = "手动" if trigger == "manual" else "自动"
        total = int(data.get("total") or 0)
        all_total = int(data.get("all_total") or total)
        eligible_total = int(data.get("eligible_total") or total)
        processed_total = int(data.get("processed_total") or total)
        limit = int(data.get("limit") or 0)
        truncated = bool(data.get("truncated"))
        ok = int(data.get("ok") or 0)
        changed = int(data.get("changed") or 0)
        unchanged = int(data.get("unchanged") or 0)
        skip = int(data.get("skip") or 0)
        fail = int(data.get("fail") or 0)
        now_text = datetime.datetime.now().strftime("%H:%M:%S")
        scope_text = f"总数 {all_total} 件，可刷新 {eligible_total} 件，本轮处理 {processed_total} 件"
        if truncated and limit > 0:
            scope_text += f"（限额 {limit}）"
        stats = f"{scope_text}；成功 {ok}（变更 {changed}，无变化 {unchanged}），跳过 {skip}，失败 {fail}"

        self._last_refresh_trigger = trigger_label
        self._last_refresh_time = now_text
        self._last_refresh_summary = f"[{trigger_label} {now_text}] {stats}"
        self._pending_refresh_report = data

        changed_samples = data.get("changed_samples") or []
        failed_samples = data.get("failed_samples") or []
        skipped_samples = data.get("skipped_samples") or []
        sample_limit = 5
        for sample in changed_samples[:sample_limit]:
            self._log(f"状态变更样例：{sample}")
        if len(changed_samples) > sample_limit:
            self._log(f"状态变更样例其余 {len(changed_samples) - sample_limit} 条已省略")
        for sample in failed_samples[:sample_limit]:
            self._log(f"刷新失败样例：{sample}")
        if len(failed_samples) > sample_limit:
            self._log(f"刷新失败样例其余 {len(failed_samples) - sample_limit} 条已省略")
        for sample in skipped_samples[:sample_limit]:
            self._log(f"刷新跳过样例：{sample}")
        if len(skipped_samples) > sample_limit:
            self._log(f"刷新跳过样例其余 {len(skipped_samples) - sample_limit} 条已省略")

        self._refresh_import_status()
        self._set_import_status_text(
            f"导入状态：{summary}｜{stats}",
            priority=self.IMPORT_STATUS_PRIORITY_REFRESH_SUMMARY,
            source="refresh_summary",
            force=True,
        )
        self._update_manual_review_button()
        if success:
            self._bridge.store_changed.emit()

    def _refresh_import_status(self):
        count = self.platform_ctx.imported_store.count()
        now_text = datetime.datetime.now().strftime("%H:%M:%S")
        text = f"商品管理范围：已导入 {count} 件商品"
        if self._last_import_summary:
            text += f" | 最近一次同步：{self._last_import_summary}"
        else:
            text += " | 自动化任务只处理这些商品"
        self._import_status_label.setText(text)
        if not self._erp_sync_running and not self._import_status_refresh_running:
            self._set_import_status_text(
                f"导入状态：已刷新（最后刷新 {now_text}）",
                priority=self.IMPORT_STATUS_PRIORITY_IDLE,
                source="idle_refresh",
            )

    def _toggle_scheduler(self, checked: bool):
        if checked:
            self.ctx.scheduler.start()
            self._log("调度器已启动")
        else:
            self.ctx.scheduler.stop()
            self._log("调度器已停止")
        self._refresh_status()

    def _toggle_task_enabled(self, task_name: str, enabled: bool):
        self.ctx.scheduler.set_enabled(task_name, enabled)
        self._log(f"{task_name} 已{'开启' if enabled else '关闭'}")
        self._refresh_status()

    def _apply_interval(self, task_name: str, interval_key: str, minutes: int):
        minutes = max(1, int(minutes or 1))
        self.ctx.scheduler.set_interval(task_name, minutes)
        cfg.set(interval_key, minutes)
        self._log(f"{task_name} 间隔已更新为 {minutes} 分钟")
        self._refresh_status()

    def _apply_stale_stage2_drop_pct(self):
        field = self._stale_stage2_drop_pct
        if field is None:
            return
        value = max(1, min(99, int(field.value() or 1)))
        field.setValue(value)
        cfg.set("stale_stage2_drop_pct", value)
        self._log(f"阶段2额外降价比例已更新为 {value}%")
        self._refresh_preview_all()

    def _run_now(self, task_name: str):
        self._log(f"手动触发: {task_name}")
        self.ctx.scheduler.run_now(task_name)

    def _refresh_status(self):
        self._scheduler_running.blockSignals(True)
        self._scheduler_running.setChecked(self.ctx.scheduler.is_running())
        self._scheduler_running.blockSignals(False)
        status = self.ctx.scheduler.get_status()
        parts = []
        for name, _label, _key, _fallback in self.TASKS:
            info = status.get(name)
            checkbox = self._task_enabled.get(name)
            if info is None:
                if checkbox is not None:
                    checkbox.blockSignals(True)
                    checkbox.setChecked(False)
                    checkbox.blockSignals(False)
                parts.append(f"⬜ {name}: 未注册")
                continue
            if checkbox is not None:
                checkbox.blockSignals(True)
                checkbox.setChecked(bool(info["enabled"]))
                checkbox.blockSignals(False)

            last = info.get("last_run") or "从未"
            next_run = info.get("next_run")
            countdown = info.get("countdown_seconds")
            duration = info.get("last_duration_seconds")
            error = str(info.get("last_error") or "").strip()
            outcome = info.get("last_outcome") or "never"

            if info.get("is_running"):
                state = "▶运行中"
                next_desc = "下次: 运行中"
            elif not info.get("enabled"):
                state = "⬜已停用"
                next_desc = "下次: 已停用"
            else:
                state = "✅已启用"
                if countdown is None:
                    next_desc = "下次: 待计算"
                else:
                    mins, secs = divmod(max(int(countdown), 0), 60)
                    next_desc = f"下次: {str(next_run)[:16] if next_run else '即将执行'}（{mins:02d}:{secs:02d}）"

            tail = []
            if duration is not None:
                tail.append(f"耗时{duration:.1f}s")
            if outcome == "error" and error:
                tail.append(f"错误:{error[:40]}")
            elif outcome == "success":
                tail.append("上次:成功")
            elif outcome == "never":
                tail.append("上次:未执行")

            extra = f" | {' | '.join(tail)}" if tail else ""
            parts.append(
                f"{state} {name}: 运行{info['run_count']}次 最后:{str(last)[:16]} | {next_desc}{extra}"
            )
        self._status_label.setText("\n".join(parts))

    def _save_custom_offset(self):
        mode = self._normalize_offset_mode(self._custom_offset_mode.currentText())
        value = self._safe_float(self._custom_offset_value.text(), 0.0)
        self._custom_offset_mode.setCurrentText(mode)
        self._custom_offset_value.setText(str(value))
        cfg.set("auto_reprice_custom_offset_mode", mode)
        cfg.set("auto_reprice_custom_offset_value", value)
        self._log(f"自动调价价格微调设置已保存：{mode} / {value}")
        self._refresh_preview_all()

    def _reset_custom_offset(self):
        self._custom_offset_mode.setCurrentText("off")
        self._custom_offset_value.setText("0.0")
        self._save_custom_offset()

    def _save_erp_config(self):
        self.ctx.erp_config.set("token", self._erp_token.text().strip())
        self.ctx.erp_config.set("version", self._erp_version.text().strip())
        self._log("ERP 配置已保存")
        QMessageBox.information(self, "保存", "ERP 配置已保存")

    def _sync_erp_import(self):
        if self._erp_sync_running:
            return
        self._set_import_running(True)
        self._set_import_status_text(
            "导入状态：准备开始 ERP 同步…",
            priority=self.IMPORT_STATUS_PRIORITY_IMPORT_START,
            source="import_start",
            force=True,
        )
        self._log("开始从 ERP 同步并导入商品 …")
        threading.Thread(target=self._do_sync_erp_import, daemon=True).start()

    def _do_sync_erp_import(self):
        try:
            result = task_erp_sync(
                self.ctx.erp_config,
                self.ctx.cost_map,
                self.ctx.account_store,
                imported_store=self.platform_ctx.imported_store,
                on_progress=self._bridge.log.emit,
                on_progress_event=self._bridge.progress.emit,
                sold_cache=self.ctx.sold_cache,
                sold_sync_days=30,
            )
            summary = result.get("summary", str(result)) if isinstance(result, dict) else str(result)
        except Exception as exc:
            summary = f"同步失败: {exc}"
            self._bridge.log.emit(summary)
        self._bridge.sync_finished.emit(summary)

    def _after_sync_erp_import(self, summary: str):
        self._set_import_running(False)
        self._last_import_summary = summary
        self._refresh_import_status()
        self._update_manual_review_button()
        self._refresh_preview_all()
        self._set_import_status_text(
            f"导入状态：{summary}",
            priority=self.IMPORT_STATUS_PRIORITY_IMPORT_DONE,
            source="import_done",
            force=True,
        )
        self._log(f"ERP 导入完成: {summary}")
        self._bridge.store_changed.emit()

    def _open_imported_window(self):
        if self._manage_dialog is None:
            self._manage_dialog = ImportedProductsDialog(self.ctx, self)
            self._manage_dialog.finished.connect(self._on_manage_dialog_closed)
        self._manage_dialog.refresh()
        self._manage_dialog.show()
        self._manage_dialog.raise_()
        self._manage_dialog.activateWindow()

    def _on_manage_dialog_closed(self, _result: int):
        self._manage_dialog = None
        self._handle_store_changed()

    def _pending_manual_review_count(self) -> int:
        items = self.platform_ctx.imported_store.get_all()
        count = 0
        for item in items:
            state = str(getattr(item, "manual_review_state", "") or "").strip().lower()
            if state == "pending" or str(getattr(item, "op_status", "") or "").strip() == "待确认":
                count += 1
        return count

    def _update_manual_review_button(self):
        count = self._pending_manual_review_count()
        self._manual_review_btn.setText(f"手动确认调价（待确认 {count}）")

    def _open_manual_review_window(self):
        if self._manual_review_dialog is None:
            self._manual_review_dialog = ManualReviewDialog(self.ctx, self)
            self._manual_review_dialog.set_store_changed_callback(self._handle_store_changed)
            self._manual_review_dialog.finished.connect(self._on_manual_review_dialog_closed)
        self._manual_review_dialog.refresh()
        self._manual_review_dialog.show()
        self._manual_review_dialog.raise_()
        self._manual_review_dialog.activateWindow()

    def _on_manual_review_dialog_closed(self, _result: int):
        self._manual_review_dialog = None
        self._handle_store_changed()

    def _on_task_finished(self, task_name: str, result):
        _ = result
        pending = self._pending_manual_review_count()
        self._update_manual_review_button()
        if pending <= 0:
            return
        self._log(f"任务 {task_name} 完成：检测到待确认 {pending} 件，已弹出人工确认窗口")
        self._open_manual_review_window()

    def _handle_store_changed(self):
        if self._manage_dialog is not None:
            self._manage_dialog.refresh()
        if self._manual_review_dialog is not None:
            self._manual_review_dialog.refresh()
        self._update_manual_review_button()
        self._refresh_import_status()
        self._refresh_preview_all()

    def _save_wx_config(self):
        for key, field in self._wx_fields.items():
            self.ctx.wxapp_config.set(key, field.text())
        self.ctx.robot_notifier.webhook_url = self._wx_fields["robot_webhook"].text()
        self._log("企业微信配置已保存")
        QMessageBox.information(self, "保存", "企业微信配置已保存")

    def _log(self, msg: str):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self._log_text.appendPlainText(f"[{timestamp}] {msg}")
        if not self._erp_sync_running and not self._import_status_refresh_running:
            self._set_import_status_text(
                f"导入状态：{msg}",
                priority=self.IMPORT_STATUS_PRIORITY_LOG,
                source="log",
            )
        scrollbar = self._log_text.verticalScrollBar()
        if scrollbar is not None:
            scrollbar.setValue(scrollbar.maximum())

    def _clear_log(self):
        self._log_text.clear()

    def closeEvent(self, event):
        self.ctx.automation_log = None
        if self._status_timer.isActive():
            self._status_timer.stop()
        if self._import_status_timer.isActive():
            self._import_status_timer.stop()
        self._manual_review_dialog = None
        self._manage_dialog = None
        super().closeEvent(event)
