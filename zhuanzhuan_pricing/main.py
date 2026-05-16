# -*- coding: utf-8 -*-
"""
转转多店极速调价中枢 v2.0
入口文件 — Qt 启动入口
"""
import sys

from PySide6.QtWidgets import QApplication, QMessageBox


def main():
    if sys.version_info < (3, 9):
        print("需要 Python 3.9+")
        sys.exit(1)

    app = QApplication(sys.argv)

    try:
        from zhuanzhuan_pricing.ui_qt.shop_cookie_window import ShopCookieMainWindow
        window = ShopCookieMainWindow()
    except Exception as e:
        import traceback
        traceback.print_exc()
        QMessageBox.critical(None, "启动失败", str(e))
        sys.exit(1)

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
