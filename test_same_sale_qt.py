from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication, QMessageBox

from zhuanzhuan_pricing.services.same_sale_service import SameSaleListing, SameSaleStore
from zhuanzhuan_pricing.ui_qt.tab_same_sale import SameSaleQtTab, filter_groups


class SameSaleQtTabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = SameSaleStore(path=str(Path(self._tmpdir.name) / "same_sale.json"))

        group = self.store.add_group(
            machine_code="QC-001",
            imei="IMEI-001",
            model="iPhone 15 Pro",
            condition="95新",
            capacity="256G",
            color="原色",
            note="主力机型",
        )
        self.store._groups[0].group_id = "SS-PRIMARY"
        self.group_id = "SS-PRIMARY"
        self.store.add_listing(
            self.group_id,
            SameSaleListing(
                platform="zhuanzhuan",
                platform_label="转转",
                product_id="ZZ-1",
                qc_code="QC-001",
                imei="IMEI-001",
                account_name="账号A",
                status="在售",
                price=4999,
            ),
        )
        self.store.add_listing(
            self.group_id,
            SameSaleListing(
                platform="xianyu",
                platform_label="闲鱼",
                product_id="XY-1",
                qc_code="QC-001",
                imei="IMEI-001",
                account_name="账号B",
                status="在售",
                price=4899,
            ),
        )

        extra = self.store.add_group(machine_code="QC-999", imei="IMEI-999", model="Mate 60")
        self.store._groups[1].group_id = "SS-SECONDARY"
        self.store.add_listing(
            "SS-SECONDARY",
            SameSaleListing(
                platform="paipai",
                platform_label="拍拍",
                product_id="PP-1",
                qc_code="QC-999",
                imei="IMEI-999",
                account_name="账号C",
                status="已售",
                sold=True,
                price=2999,
            ),
        )

        self.ctx = SimpleNamespace(same_sale_store=self.store)
        self.tab = SameSaleQtTab(self.ctx)

    def tearDown(self):
        self.tab.deleteLater()
        self._tmpdir.cleanup()

    def test_filter_groups_matches_group_model_qc_and_imei(self):
        groups = self.store.get_all()
        self.assertEqual(len(filter_groups(groups, keyword="iphone")), 1)
        self.assertEqual(len(filter_groups(groups, keyword=self.group_id)), 1)
        self.assertEqual(len(filter_groups(groups, keyword="QC-001")), 1)
        self.assertEqual(len(filter_groups(groups, keyword="imei-001")), 1)

    def test_select_group_renders_detail_and_listings(self):
        self.tab._group_table.selectRow(0)
        self.tab._on_group_selected()

        self.assertIn(self.group_id, self.tab._summary_label.text())
        self.assertIn("iPhone 15 Pro", self.tab._summary_label.text())
        self.assertEqual(self.tab._listing_table.rowCount(), 2)
        self.assertEqual(self.tab._listing_table.item(0, 0).text(), "转转")

    def test_mark_sold_refreshes_store_state(self):
        original_question = QMessageBox.question
        QMessageBox.question = lambda *args, **kwargs: QMessageBox.StandardButton.Yes
        try:
            self.tab._group_table.selectRow(0)
            self.tab._on_group_selected()
            self.tab._listing_table.selectRow(0)
            self.tab._on_listing_selected()
            self.tab._mark_sold()
        finally:
            QMessageBox.question = original_question

        group = next(group for group in self.store.get_all() if group.group_id == self.group_id)
        sold_listing = next(listing for listing in group.listings if listing.platform == "zhuanzhuan")
        other_listing = next(listing for listing in group.listings if listing.platform == "xianyu")
        self.assertTrue(sold_listing.sold)
        self.assertEqual(sold_listing.status, "已售")
        self.assertTrue(other_listing.delist_pending)
        self.assertIn("已标记为已售", self.tab._status_label.text())

    def test_mark_delisted_refreshes_store_state(self):
        self.store.mark_sold(self.group_id, "zhuanzhuan", product_id="ZZ-1", qc_code="QC-001", imei="IMEI-001")
        self.tab.refresh()
        self.tab._group_table.selectRow(0)
        self.tab._on_group_selected()
        self.tab._listing_table.selectRow(1)
        self.tab._on_listing_selected()
        self.tab._mark_delisted()

        group = next(group for group in self.store.get_all() if group.group_id == self.group_id)
        listing = next(listing for listing in group.listings if listing.platform == "xianyu")
        self.assertTrue(listing.delisted)
        self.assertFalse(listing.delist_pending)
        self.assertEqual(listing.status, "已下架")
        self.assertIn("已标记为已下架", self.tab._status_label.text())

    def test_action_without_selected_listing_sets_status_message(self):
        self.tab._selected_group_id = ""
        self.tab._selected_listing_identity = None
        self.tab._mark_delisted()
        self.assertIn("未选择挂单", self.tab._status_label.text())


if __name__ == "__main__":
    unittest.main()
