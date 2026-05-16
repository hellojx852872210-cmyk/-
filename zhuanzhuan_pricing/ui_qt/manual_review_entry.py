# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox


def main() -> int:
    app = QApplication(sys.argv)
    try:
        from zhuanzhuan_pricing.ui.app import AppContext
        from zhuanzhuan_pricing.ui_qt.manual_review_window import ManualReviewDialog

        ctx = AppContext()
        runtime_pool = Path(__file__).resolve().parents[2] / "runtime" / "imported_items_pool.json"
        if runtime_pool.exists():
            try:
                ctx.zhuanzhuan.imported_store.load_from_file(str(runtime_pool))
            except Exception:
                pass
        window = ManualReviewDialog(ctx)
    except Exception as exc:
        import traceback

        traceback.print_exc()
        QMessageBox.critical(None, "启动失败", str(exc))
        return 1

    window.show()
    window.raise_()
    window.activateWindow()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
