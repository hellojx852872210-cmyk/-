# -*- coding: utf-8 -*-
"""
主窗口骨架 + AppContext（依赖注入容器）

兼容说明：本模块保留为 Tk 兼容壳，主入口以 Qt 为准（zhuanzhuan_pricing/main.py）。
新功能默认落 Qt 与 services/automation/core，不在此处扩展。
"""
from __future__ import annotations
import re
import tkinter as tk
from tkinter import ttk
import logging
from typing import Callable

from ..config import cfg
from ..core.pricing_engine import PricingEngine
from ..core.rule_engine import RuleEngine
from ..core.price_history import get_history_db
from ..core.batch_store import BatchItemStore
from ..services.data_store import AccountStore, SoldCache, CostPriceMap
from ..services.erp_service import ErpConfig, ErpFetcher
from ..services.notify_service import WxAppConfig, WxAppClient, WxRobotNotifier
from ..services.paipai_service import PaipaiConfig, PaipaiClient
from ..services.same_sale_service import SameSaleStore
from ..services.browser_instance_store import BrowserInstanceStore
from ..automation.scheduler import Scheduler
from ..browser.manager import BrowserProfileManager
from ..browser.session import BrowserSessionRegistry, PlatformBrowserContext

logger = logging.getLogger(__name__)


def _make_automation_progress(ctx: "AppContext") -> Callable[[str], None]:
    def automation_progress(msg: str):
        callback = getattr(ctx, "automation_log", None)
        if callable(callback):
            try:
                callback(msg)
            except Exception:
                pass

    return automation_progress


def _notify_automation_task_finished(ctx: "AppContext", task_name: str, result):
    callback = getattr(ctx, "automation_task_finished", None)
    if callable(callback):
        try:
            callback(task_name, result)
        except Exception:
            pass


def register_auto_tasks(ctx: "AppContext") -> None:
    if getattr(ctx, "_auto_tasks_registered", False):
        return

    from ..automation.tasks import task_auto_reprice, task_stale_drop, task_auto_list, task_probe_perturbation, task_sales_report

    automation_progress = _make_automation_progress(ctx)

    def reprice_task():
        automation_progress("开始执行自动调价（仅导入商品）")
        result = task_auto_reprice(
            ctx.account_store,
            ctx.sold_cache,
            ctx.cost_map,
            ctx.rule_engine,
            ctx.zhuanzhuan.imported_store,
            on_progress=automation_progress,
        )
        automation_progress(f"自动调价完成: {result}")
        _notify_automation_task_finished(ctx, "auto_reprice", result)
        return result

    def stale_task():
        automation_progress("开始执行滞销降价（仅导入商品）")
        result = task_stale_drop(
            ctx.account_store,
            ctx.sold_cache,
            ctx.rule_engine,
            ctx.zhuanzhuan.imported_store,
            on_progress=automation_progress,
        )
        automation_progress(f"滞销降价完成: {result}")
        _notify_automation_task_finished(ctx, "stale_drop", result)
        return result

    def report_task():
        automation_progress("开始执行销售播报")
        result = task_sales_report(
            ctx.account_store,
            ctx.robot_notifier,
            on_progress=automation_progress,
            imported_store=ctx.zhuanzhuan.imported_store,
            wx_app_client=ctx.wxapp_client,
        )
        automation_progress("销售播报完成")
        _notify_automation_task_finished(ctx, "sales_report", result)
        return result

    def probe_task():
        automation_progress("开始执行探针扰动（独立任务）")
        result = task_probe_perturbation(
            ctx.account_store,
            ctx.sold_cache,
            ctx.rule_engine,
            ctx.zhuanzhuan.imported_store,
            on_progress=automation_progress,
        )
        automation_progress(f"探针扰动完成: {result}")
        _notify_automation_task_finished(ctx, "probe_perturbation", result)
        return result

    def auto_list_task():
        automation_progress("开始执行未上架自动上架（仅导入商品）")
        result = task_auto_list(
            ctx.account_store,
            ctx.erp_config,
            ctx.sold_cache,
            ctx.rule_engine,
            ctx.zhuanzhuan.imported_store,
            on_progress=automation_progress,
        )
        automation_progress(f"未上架自动上架完成: {result}")
        _notify_automation_task_finished(ctx, "auto_list", result)
        return result

    ctx.scheduler.register(
        "auto_reprice", reprice_task,
        interval_minutes=cfg.auto_reprice_interval,
        enabled=False,
    )
    ctx.scheduler.register(
        "stale_drop", stale_task,
        interval_minutes=cfg.auto_reprice_interval,
        enabled=False,
    )
    ctx.scheduler.register(
        "auto_list", auto_list_task,
        interval_minutes=cfg.auto_list_interval,
        enabled=False,
    )
    ctx.scheduler.register(
        "sales_report", report_task,
        interval_minutes=cfg.sales_report_interval,
        enabled=False,
    )
    ctx.scheduler.register(
        "probe_perturbation", probe_task,
        interval_minutes=cfg.probe_interval_minutes,
        enabled=False,
    )
    ctx._auto_tasks_registered = True


class ZhuanzhuanContext:
    """转转模块上下文，只暴露转转子模块需要的能力。"""

    def __init__(self, app_ctx: "AppContext"):
        self._app_ctx = app_ctx
        self.account_store = app_ctx.account_store
        self.sold_cache = app_ctx.sold_cache
        self.cost_map = app_ctx.cost_map
        self.pricing_engine = app_ctx.pricing_engine
        self.rule_engine = app_ctx.rule_engine
        self.history_db = app_ctx.history_db
        self.erp_config = app_ctx.erp_config
        self.erp_fetcher = app_ctx.erp_fetcher
        self.batch_store = BatchItemStore()
        self.imported_store = BatchItemStore()
        self.browser_instance_store = app_ctx.browser_instance_store
        self.browser_sessions = PlatformBrowserContext(
            app_ctx.browser_sessions,
            "zhuanzhuan",
            ("fetch", "imei", "batch"),
        )

    def cfg_val(self, key, default=None):
        return self._app_ctx.cfg_val(key, default)


class AppContext:
    """
    应用上下文 — 所有单例服务的持有者
    Tab 通过 ctx 访问服务，不直接实例化
    """

    def __init__(self):
        # 数据层
        self.account_store = AccountStore()
        self.sold_cache = SoldCache()
        self.cost_map = CostPriceMap()

        # 引擎层
        self.pricing_engine = PricingEngine()
        self.rule_engine = RuleEngine()
        self.history_db = get_history_db()

        # ERP
        self.erp_config = ErpConfig()
        self.erp_fetcher = ErpFetcher(self.erp_config)

        # 拍拍
        self.paipai_config = PaipaiConfig()
        self.paipai_client = PaipaiClient(self.paipai_config)

        # 同售管理
        self.same_sale_store = SameSaleStore()

        # 通知
        self.wxapp_config = WxAppConfig()
        self.wxapp_client = WxAppClient(self.wxapp_config)
        webhook = self.wxapp_config.get("robot_webhook", "")
        self.robot_notifier = WxRobotNotifier(webhook)

        # 浏览器会话层
        self.browser_profile_manager = BrowserProfileManager()
        self.browser_sessions = BrowserSessionRegistry(self.browser_profile_manager)
        self.browser_instance_store = BrowserInstanceStore()

        # 自动化日志/事件回调
        self.automation_log = None
        self.automation_task_finished = None
        self._auto_tasks_registered = False

        # 平台模块状态
        self.zhuanzhuan = ZhuanzhuanContext(self)

        # 调度器
        self.scheduler = Scheduler(tick_seconds=60)

    def register_auto_tasks(self):
        register_auto_tasks(self)


class PricingAssistantApp:
    """主应用窗口"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("多平台同售管理中枢 v2.0")
        self.root.geometry("1280x800")
        self.root.minsize(1024, 640)

        # 初始化上下文
        self.ctx = AppContext()

        # 设置日志输出到 UI
        self._setup_logging()

        # 构建 UI
        self._build_menu()
        self._build_notebook()

        # 启动调度器
        self.ctx.scheduler.start()
        self._register_auto_tasks()

        # 关闭时清理
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── 菜单 ─────────────────────────────────────────────────

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        # 工具菜单
        tools_menu = tk.Menu(menubar, tearoff=False)
        menubar.add_cascade(label="工具", menu=tools_menu)
        tools_menu.add_command(label="⚙️ 规则中心", command=lambda: self._select_tab("rules_center"))
        tools_menu.add_command(label="🔗 同售管理", command=lambda: self._select_tab("same_sale"))
        tools_menu.add_command(label="📊 数据看板", command=lambda: self._select_tab("dashboard"))
        tools_menu.add_command(label="📋 历史记录", command=lambda: self._select_tab("history"))
        tools_menu.add_separator()
        tools_menu.add_command(label="🔧 全局设置", command=self._open_settings)

        # 帮助菜单
        help_menu = tk.Menu(menubar, tearoff=False)
        menubar.add_cascade(label="帮助", menu=help_menu)
        help_menu.add_command(label="使用说明", command=self._show_help)

    # ── Notebook ─────────────────────────────────────────────

    def _build_notebook(self):
        self._notebook = ttk.Notebook(self.root)
        self._notebook.pack(fill="both", expand=True, padx=4, pady=4)

        # 延迟导入 Tab（避免循环依赖）
        from .tab_zhuanzhuan import ZhuanzhuanTab
        from .tab_paipai import PaipaiTab
        from .tab_xianyu import XianyuTab
        from .tab_95fen import Fen95Tab
        from .tab_auto import AutoTab
        from .tab_rules import RulesCenterTab
        from .tab_same_sale import SameSaleTab
        from .tab_dashboard import DashboardTab
        from .tab_history import HistoryTab

        self._tabs = {}
        for name, tab_class in (
            ("zhuanzhuan", ZhuanzhuanTab),
            ("paipai", PaipaiTab),
            ("xianyu", XianyuTab),
            ("95fen", Fen95Tab),
            ("auto", AutoTab),
            ("rules_center", RulesCenterTab),
            ("same_sale", SameSaleTab),
            ("dashboard", DashboardTab),
            ("history", HistoryTab),
        ):
            self._tabs[name] = self._safe_create_tab(name, tab_class)

    def _safe_create_tab(self, tab_name: str, tab_class):
        try:
            return tab_class(self._notebook, self.ctx)
        except Exception as exc:
            logger.exception("顶层页初始化失败: %s", tab_name)
            return _AppTabInitErrorPane(self._notebook, tab_name=tab_name, detail=str(exc))

    def _select_tab(self, tab_name: str):
        tab = self._tabs.get(tab_name)
        if tab:
            self._notebook.select(tab.frame)

    # ── 自动化任务注册 ────────────────────────────────────────

    def _register_auto_tasks(self):
        self.ctx.register_auto_tasks()

    # ── 弹窗 ─────────────────────────────────────────────────

    def _open_settings(self):
        from .windows.settings_window import SettingsWindow
        SettingsWindow(self.root, self.ctx)

    def _show_help(self):
        import webbrowser
        webbrowser.open("https://github.com/your-repo/zhuanzhuan-pricing")

    # ── 日志 ─────────────────────────────────────────────────

    def _setup_logging(self):
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )

    # ── 关闭 ─────────────────────────────────────────────────

    def _on_close(self):
        self.ctx.scheduler.stop()
        self.root.destroy()


class _AppTabInitErrorPane:
    def __init__(self, parent: ttk.Notebook, *, tab_name: str, detail: str):
        self.frame = ttk.Frame(parent)
        parent.add(self.frame, text=f"⚠️ {tab_name}")
        card = ttk.LabelFrame(self.frame, text=f"{tab_name} 模块初始化失败")
        card.pack(fill="both", expand=True, padx=12, pady=12)
        ttk.Label(
            card,
            text="该模块当前不可用，但不会阻塞其它顶层模块启动。",
            foreground="#b26a00",
        ).pack(anchor="w", padx=12, pady=(12, 6))
        text = tk.Text(card, height=10, wrap="word")
        text.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        text.insert("1.0", detail or "未知错误")
        text.config(state="disabled")


# 给 AppContext 添加 cfg_val 辅助方法
def _app_ctx_cfg_val(self, key, default=None):
    from ..config import cfg
    return cfg.get(key, default)


AppContext.cfg_val = _app_ctx_cfg_val
