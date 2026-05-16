# -*- coding: utf-8 -*-
from __future__ import annotations

import csv
import datetime
import threading

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFileDialog,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


class HistoryQtTab(QWidget):
    TRIGGER_OPTIONS = ["", "manual", "auto_reprice", "auto_stale", "auto_list", "wxapp_cmd"]
    TRIGGER_LABELS = {
        "manual": "手动",
        "auto_reprice": "自动调价",
        "auto_stale": "滞销降价",
        "auto_list": "自动上架",
        "wxapp_cmd": "微信指令",
    }
    COLUMNS = [
        ("timestamp", "改价时间"),
        ("qc_code", "质检码"),
        ("title", "商品名"),
        ("model", "型号"),
        ("old_price", "改前价"),
        ("new_price", "改后价"),
        ("diff", "差价"),
        ("diff_pct", "降幅%"),
        ("settle_price", "改后预计到手"),
        ("trigger", "触发"),
        ("account_name", "账号"),
        ("note", "备注"),
    ]

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self._records = []
        self._query_in_progress = False
        self._query_result = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        intro = QLabel("查询和导出改价历史。当前 Qt 页复用原有 history_db 查询口径，支持筛选、异步查询、表格浏览和 CSV 导出。")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(self._build_filter_box())
        layout.addWidget(self._build_table_box(), 1)

        self._status_label = QLabel("点击“查询”加载记录。")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

    def _build_filter_box(self):
        box = QGroupBox("筛选条件")
        layout = QGridLayout(box)

        layout.addWidget(QLabel("型号"), 0, 0)
        self._model_input = QLineEdit()
        self._model_input.returnPressed.connect(self._query)
        layout.addWidget(self._model_input, 0, 1)

        layout.addWidget(QLabel("账号"), 0, 2)
        self._account_input = QLineEdit()
        self._account_input.returnPressed.connect(self._query)
        layout.addWidget(self._account_input, 0, 3)

        layout.addWidget(QLabel("触发方式"), 0, 4)
        self._trigger_combo = QComboBox()
        self._trigger_combo.addItems(self.TRIGGER_OPTIONS)
        layout.addWidget(self._trigger_combo, 0, 5)

        layout.addWidget(QLabel("近 N 天"), 0, 6)
        self._days_input = QSpinBox()
        self._days_input.setRange(1, 365)
        self._days_input.setValue(30)
        layout.addWidget(self._days_input, 0, 7)

        btn_row = QHBoxLayout()
        self._query_btn = QPushButton("查询")
        export_btn = QPushButton("导出 CSV")
        btn_row.addWidget(self._query_btn)
        btn_row.addWidget(export_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row, 0, 8, 1, 2)

        self._query_btn.clicked.connect(self._query)
        export_btn.clicked.connect(self._export_csv)

        return box

    def _build_table_box(self):
        box = QGroupBox("查询结果")
        layout = QVBoxLayout(box)

        self._table = QTableWidget(0, len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels([label for _, label in self.COLUMNS])
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._table, 1)

        return box

    def _query(self):
        if self._query_in_progress:
            return
        self._query_in_progress = True
        self._query_result = None
        self._query_btn.setEnabled(False)
        self._status_label.setText("查询中 …")
        threading.Thread(target=self._do_query, daemon=True).start()
        QTimer.singleShot(150, self._poll_query_result)

    def _do_query(self):
        try:
            records = self.ctx.history_db.query(
                model=self._model_input.text().strip(),
                account=self._account_input.text().strip(),
                trigger=self._trigger_combo.currentText().strip(),
                days=self._days_input.value(),
                limit=1000,
            )
            self._query_result = {"records": records}
        except Exception as exc:
            self._query_result = {"error": str(exc)}

    def _poll_query_result(self):
        if self._query_result is None:
            QTimer.singleShot(150, self._poll_query_result)
            return

        result = self._query_result
        self._query_result = None
        self._query_in_progress = False
        self._query_btn.setEnabled(True)

        if result.get("error"):
            self._status_label.setText(f"查询失败: {result['error']}")
            QMessageBox.critical(self, "查询失败", result["error"])
            return

        self._records = result.get("records", [])
        self._render(self._records)

    def _render(self, records):
        self._table.setRowCount(len(records))
        for row, record in enumerate(records):
            trigger_value = record.trigger.value if hasattr(record.trigger, "value") else str(record.trigger)
            values = [
                record.timestamp.strftime("%m-%d %H:%M"),
                record.qc_code,
                record.title[:20],
                record.model[:14],
                f"¥{record.old_price:,.0f}",
                f"¥{record.new_price:,.0f}",
                f"{record.diff:+.0f}",
                f"{record.diff_pct:+.1f}%",
                f"¥{record.settle_price:,.0f}",
                self.TRIGGER_LABELS.get(trigger_value, trigger_value),
                record.account_name,
                record.note,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if record.diff < 0:
                    item.setBackground(QColor("#fff8e8"))
                elif record.diff > 0:
                    item.setBackground(QColor("#f2f8f2"))
                self._table.setItem(row, column, item)

        if records:
            self._status_label.setText(f"共 {len(records)} 条记录")
        else:
            self._status_label.setText("查询完成，当前条件下暂无记录")

    def _export_csv(self):
        if not self._records:
            self._query()
            return

        default_name = f"改价历史_{datetime.date.today()}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "导出 CSV", default_name, "CSV Files (*.csv)")
        if not path:
            return

        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as file:
                writer = csv.writer(file)
                writer.writerow([
                    "时间", "质检码", "商品名", "型号", "成色", "容量", "颜色",
                    "改前价", "改后价", "差价", "降幅%", "改后预计到手",
                    "触发方式", "账号", "备注",
                ])
                for record in self._records:
                    trigger_value = record.trigger.value if hasattr(record.trigger, "value") else record.trigger
                    writer.writerow([
                        record.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                        record.qc_code,
                        record.title,
                        record.model,
                        record.condition,
                        record.capacity,
                        record.color,
                        record.old_price,
                        record.new_price,
                        record.diff,
                        f"{record.diff_pct:.2f}",
                        record.settle_price,
                        trigger_value,
                        record.account_name,
                        record.note,
                    ])
        except Exception as exc:
            self._status_label.setText(f"导出失败: {exc}")
            QMessageBox.critical(self, "导出失败", str(exc))
            return

        self._status_label.setText(f"已导出 {path}")
        QMessageBox.information(self, "导出成功", f"已保存到\n{path}")
