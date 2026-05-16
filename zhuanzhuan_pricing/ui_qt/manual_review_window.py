# -*- coding: utf-8 -*-
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..automation.tasks import apply_manual_review_batch_decision, apply_manual_review_decision


class ManualReviewDialog(QDialog):
    COL_ACCOUNT = 0
    COL_QC = 1
    COL_IMEI = 2
    COL_TITLE = 3
    COL_SOURCE = 4
    COL_CURRENT_PRICE = 5
    COL_TARGET_PRICE = 6
    COL_COST_PROFIT = 7
    COL_REASON = 8
    COL_RESULT = 9

    SOURCE_LABELS = {
        "auto_reprice": "自动调价",
        "stale_drop": "滞销降价",
        "auto_list": "自动上架",
    }

    def __init__(self, ctx, parent: QWidget | None = None):
        super().__init__(parent)
        self.ctx = ctx
        self._on_store_changed = None
        self.setWindowTitle("手动确认调价")
        self.resize(920, 540)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self.setModal(False)
        self._build_ui()
        self.refresh()

    def set_store_changed_callback(self, callback):
        self._on_store_changed = callback

    def _imported_store(self):
        if hasattr(self.ctx, "zhuanzhuan") and hasattr(self.ctx.zhuanzhuan, "imported_store"):
            return self.ctx.zhuanzhuan.imported_store
        return getattr(self.ctx, "imported_store")

    def _emit_store_changed(self):
        if callable(self._on_store_changed):
            self._on_store_changed()

    def _is_pending(self, item) -> bool:
        state = str(getattr(item, "manual_review_state", "") or "").strip().lower()
        if state == "pending":
            return True
        return str(getattr(item, "op_status", "") or "").strip() == "待确认"

    def _pending_items(self):
        return [item for item in self._imported_store().get_all() if self._is_pending(item)]

    def _build_ui(self):
        layout = QVBoxLayout(self)
        hint = QLabel("集中处理所有待人工确认项：支持 接受调价 / 改价后执行 / 拒绝。")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        toolbar = QHBoxLayout()
        self._refresh_btn = QPushButton("刷新")
        self._minimize_btn = QPushButton("缩小")
        self._select_all_btn = QPushButton("全选")
        self._clear_selection_btn = QPushButton("取消选择")
        self._accept_btn = QPushButton("接受调价")
        self._accept_custom_btn = QPushButton("改价后执行")
        self._reject_btn = QPushButton("拒绝")
        toolbar.addWidget(self._refresh_btn)
        toolbar.addWidget(self._minimize_btn)
        toolbar.addWidget(self._select_all_btn)
        toolbar.addWidget(self._clear_selection_btn)
        toolbar.addWidget(self._accept_btn)
        toolbar.addWidget(self._accept_custom_btn)
        toolbar.addWidget(self._reject_btn)
        toolbar.addStretch(1)
        self._summary_label = QLabel("待确认 0 件")
        toolbar.addWidget(self._summary_label)
        layout.addLayout(toolbar)

        self._table = QTableWidget(0, 10)
        self._table.setHorizontalHeaderLabels([
            "账号",
            "质检码",
            "IMEI",
            "商品名",
            "来源",
            "当前价",
            "建议执行价",
            "成本/利润",
            "确认原因",
            "处理结果",
        ])
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._table, 1)

        self._refresh_btn.clicked.connect(self.refresh)
        self._minimize_btn.clicked.connect(self.showMinimized)
        self._select_all_btn.clicked.connect(self._select_all_rows)
        self._clear_selection_btn.clicked.connect(self._clear_selection)
        self._accept_btn.clicked.connect(lambda: self._apply_action("accept", "确认对选中项执行接受调价？"))
        self._accept_custom_btn.clicked.connect(self._apply_accept_with_custom_price)
        self._reject_btn.clicked.connect(lambda: self._apply_action("reject", "确认拒绝选中项？"))

    def _selected_product_ids(self) -> list[str]:
        rows = sorted({index.row() for index in self._table.selectionModel().selectedRows()})
        ids: list[str] = []
        for row in rows:
            cell = self._table.item(row, self.COL_QC)
            pid = str(cell.data(Qt.ItemDataRole.UserRole) if cell is not None else "")
            if pid:
                ids.append(pid)
        return ids

    def _select_all_rows(self):
        if self._table.rowCount() <= 0:
            return
        self._table.selectAll()

    def _clear_selection(self):
        self._table.clearSelection()

    def refresh(self):
        items = self._pending_items()
        self._table.setRowCount(len(items))
        for row, item in enumerate(items):
            source = str(getattr(item, "manual_review_source", "") or "").strip()
            source_label = self.SOURCE_LABELS.get(source, "人工确认")
            reason = str(getattr(item, "manual_review_reason", "") or "").strip() or "—"
            result = str(getattr(item, "op_message", "") or getattr(item, "reprice_msg", "") or "—")
            current_price_value = float(getattr(item, "current_price", 0) or 0)
            current_price = f"¥{current_price_value:,.0f}"
            target_price = getattr(item, "manual_review_target_price", None)
            if target_price is None:
                target_price = getattr(item, "new_price", None)
            if target_price is None:
                target_price = getattr(item, "suggested_price", None)
            target_text = f"¥{float(target_price):,.0f}" if target_price is not None else "—"
            imei_text = str(getattr(item, "imei", "") or "—")

            cost_price = getattr(item, "cost_price", None)
            cost_profit_text = "—"
            if cost_price is not None:
                try:
                    cost_val = float(cost_price)
                    profit = None if target_price is None else float(target_price) - cost_val
                    if profit is None:
                        cost_profit_text = f"成本 ¥{cost_val:,.0f}"
                    else:
                        profit_rate = (profit / cost_val * 100.0) if cost_val > 0 else 0.0
                        cost_profit_text = f"成本 ¥{cost_val:,.0f} / 利润 {profit:+.0f} ({profit_rate:+.1f}%)"
                except Exception:
                    cost_profit_text = "—"

            account_item = QTableWidgetItem(item.account_name or "—")
            qc_item = QTableWidgetItem(item.qc_code or "—")
            qc_item.setData(Qt.ItemDataRole.UserRole, item.product_id)
            imei_item = QTableWidgetItem(imei_text)
            title_item = QTableWidgetItem(item.title or item.model or "—")
            source_item = QTableWidgetItem(source_label)
            current_item = QTableWidgetItem(current_price)
            target_item = QTableWidgetItem(target_text)
            cost_profit_item = QTableWidgetItem(cost_profit_text)
            reason_item = QTableWidgetItem(reason)
            result_item = QTableWidgetItem(result)
            for col, cell in enumerate((account_item, qc_item, imei_item, title_item, source_item, current_item, target_item, cost_profit_item, reason_item, result_item)):
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row, col, cell)
        self._summary_label.setText(f"待确认 {len(items)} 件")

    def _apply_action(self, action: str, confirm_text: str):
        ids = self._selected_product_ids()
        if not ids:
            QMessageBox.information(self, "提示", "请先选择待处理商品")
            return
        if QMessageBox.question(self, "确认", f"{confirm_text}\n\n数量：{len(ids)}") != QMessageBox.StandardButton.Yes:
            return
        result = apply_manual_review_batch_decision(
            self.ctx.account_store,
            self._imported_store(),
            ids,
            action,
        )
        ok = int(result.get("ok") or 0)
        fail = int(result.get("fail") or 0)
        fail_msgs = list(result.get("fail_messages") or [])
        self.refresh()
        self._emit_store_changed()
        if fail <= 0:
            QMessageBox.information(self, "完成", f"处理完成：成功 {ok}")
            return
        detail = "\n".join(fail_msgs[:6])
        if len(fail_msgs) > 6:
            detail += f"\n其余 {len(fail_msgs) - 6} 条已省略"
        QMessageBox.warning(self, "部分失败", f"处理完成：成功 {ok}，失败 {fail}\n\n{detail}")

    def _apply_accept_with_custom_price(self):
        ids = self._selected_product_ids()
        if not ids:
            QMessageBox.information(self, "提示", "请先选择待处理商品")
            return
        text, ok = QInputDialog.getText(self, "改价后执行", "请输入执行价格（元）:")
        if not ok:
            return
        try:
            custom_price = float(str(text or "").strip())
        except Exception:
            QMessageBox.warning(self, "输入错误", "请输入有效数字价格")
            return
        if custom_price <= 0:
            QMessageBox.warning(self, "输入错误", "执行价格必须大于 0")
            return
        if QMessageBox.question(self, "确认", f"确认按 ¥{custom_price:,.0f} 执行选中 {len(ids)} 件商品？") != QMessageBox.StandardButton.Yes:
            return

        store = self._imported_store()
        ok_count = 0
        fail_count = 0
        fail_msgs: list[str] = []

        for pid in ids:
            success, msg = apply_manual_review_decision(
                self.ctx.account_store,
                store,
                pid,
                "accept",
                target_price_override=custom_price,
            )
            if success:
                ok_count += 1
            else:
                fail_count += 1
                fail_msgs.append(f"{pid}: {msg}")

        self.refresh()
        self._emit_store_changed()
        if fail_count <= 0:
            QMessageBox.information(self, "完成", f"处理完成：成功 {ok_count}")
            return
        detail = "\n".join(fail_msgs[:6])
        if len(fail_msgs) > 6:
            detail += f"\n其余 {len(fail_msgs) - 6} 条已省略"
        QMessageBox.warning(self, "部分失败", f"处理完成：成功 {ok_count}，失败 {fail_count}\n\n{detail}")
