# -*- coding: utf-8 -*-
"""
数据看板 Tab — matplotlib 内嵌 Tkinter
展示：今日销售统计、库存结构、改价趋势、型号改价排行
"""
from __future__ import annotations
import tkinter as tk
from tkinter import ttk
import datetime
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import AppContext

try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    HAS_MPL = True
except ImportError:
    HAS_MPL = False


def _configure_matplotlib_fonts():
    candidate_fonts = [
        "PingFang SC",
        "Hiragino Sans GB",
        "Songti SC",
        "STHeiti",
        "Heiti SC",
        "Arial Unicode MS",
        "Microsoft YaHei",
        "Noto Sans CJK SC",
        "WenQuanYi Zen Hei",
        "SimHei",
    ]
    available = {f.name for f in font_manager.fontManager.ttflist}
    selected = [name for name in candidate_fonts if name in available]
    if selected:
        plt.rcParams["font.family"] = "sans-serif"
        plt.rcParams["font.sans-serif"] = selected
    plt.rcParams["axes.unicode_minus"] = False


if HAS_MPL:
    _configure_matplotlib_fonts()


class DashboardTab:
    """数据看板"""

    def __init__(self, parent: ttk.Notebook, ctx: "AppContext"):
        self.ctx = ctx
        self.frame = ttk.Frame(parent)
        parent.add(self.frame, text="📊 数据看板")
        self._build_ui()

    # ── UI 构建 ──────────────────────────────────────────────

    def _build_ui(self):
        if not HAS_MPL:
            ttk.Label(
                self.frame,
                text="⚠️ 数据看板需要 matplotlib\n\n运行：pip install matplotlib",
                font=("微软雅黑", 14),
            ).pack(expand=True)
            return

        # 顶部工具栏
        toolbar = ttk.Frame(self.frame)
        toolbar.pack(fill="x", padx=10, pady=5)

        ttk.Label(toolbar, text="时间范围：").pack(side="left")
        self._days_var = tk.IntVar(value=7)
        for d, label in [(1, "今日"), (7, "近7天"), (30, "近30天")]:
            ttk.Radiobutton(
                toolbar, text=label,
                variable=self._days_var, value=d,
                command=self.refresh,
            ).pack(side="left", padx=4)

        ttk.Button(toolbar, text="🔄 刷新", command=self.refresh).pack(side="right")

        # KPI 指标行
        kpi_frame = ttk.LabelFrame(self.frame, text="核心指标")
        kpi_frame.pack(fill="x", padx=10, pady=4)
        self._kpi_labels: dict[str, tk.StringVar] = {}
        for col, (key, label) in enumerate([
            ("sold_count",    "成交台数"),
            ("sold_amount",   "成交金额"),
            ("avg_price",     "平均成交价"),
            ("reprice_count", "改价次数"),
            ("avg_drop",      "平均降幅"),
        ]):
            var = tk.StringVar(value="—")
            self._kpi_labels[key] = var
            f = ttk.Frame(kpi_frame)
            f.grid(row=0, column=col, padx=20, pady=6)
            ttk.Label(f, text=label, foreground="gray").pack()
            ttk.Label(f, textvariable=var, font=("微软雅黑", 16, "bold")).pack()

        # 图表区域（2×2）
        charts_frame = ttk.Frame(self.frame)
        charts_frame.pack(fill="both", expand=True, padx=10, pady=4)

        self._fig = Figure(figsize=(12, 6), dpi=90, facecolor="#f5f5f5")
        self._fig.subplots_adjust(hspace=0.4, wspace=0.35)

        self._ax_trend    = self._fig.add_subplot(2, 2, 1)  # 成交价走势
        self._ax_stale    = self._fig.add_subplot(2, 2, 2)  # 在架天数分布
        self._ax_reprice  = self._fig.add_subplot(2, 2, 3)  # 每日改价次数
        self._ax_model    = self._fig.add_subplot(2, 2, 4)  # 型号改价排行

        self._canvas = FigureCanvasTkAgg(self._fig, master=charts_frame)
        self._canvas.get_tk_widget().pack(fill="both", expand=True)

        # 底部状态
        self._status_var = tk.StringVar(value="点击「刷新」加载数据")
        ttk.Label(self.frame, textvariable=self._status_var,
                  foreground="gray").pack(pady=3)

        # 首次加载
        self.frame.after(500, self.refresh)

    def refresh(self):
        """后台线程刷新所有图表"""
        if not HAS_MPL:
            return
        self._status_var.set("加载中 …")
        threading.Thread(target=self._do_refresh, daemon=True).start()

    def _do_refresh(self):
        try:
            days = self._days_var.get()
            self._update_kpi(days)
            self._plot_all(days)
            self._status_var.set(
                f"最后更新：{datetime.datetime.now().strftime('%H:%M:%S')}"
            )
        except Exception as e:
            self._status_var.set(f"加载失败：{e}")

    # ── KPI ──────────────────────────────────────────────────

    def _update_kpi(self, days: int):
        sold_cache = self.ctx.sold_cache
        history_db = self.ctx.history_db

        records = sold_cache.load()
        cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
        recent = [r for r in records if r.sold_time >= cutoff]

        count = len(recent)
        amount = sum(r.sold_price for r in recent)
        avg = amount / count if count else 0

        changes = history_db.query(days=days, limit=9999)
        drops = [c.diff for c in changes if c.diff < 0]
        avg_drop = sum(drops) / len(drops) if drops else 0

        def upd():
            self._kpi_labels["sold_count"].set(f"{count} 台")
            self._kpi_labels["sold_amount"].set(f"¥{amount:,.0f}")
            self._kpi_labels["avg_price"].set(f"¥{avg:,.0f}")
            self._kpi_labels["reprice_count"].set(f"{len(changes)} 次")
            self._kpi_labels["avg_drop"].set(f"¥{avg_drop:,.0f}")

        self.frame.after(0, upd)

    # ── 图表 ──────────────────────────────────────────────────

    def _plot_all(self, days: int):
        sold_cache = self.ctx.sold_cache
        history_db = self.ctx.history_db

        records = sold_cache.load()
        cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
        recent = sorted(
            [r for r in records if r.sold_time >= cutoff],
            key=lambda r: r.sold_time,
        )
        daily_summary = history_db.daily_summary(days=days)
        model_summary = history_db.model_summary(days=days, limit=8)

        def draw():
            self._ax_trend.clear()
            self._ax_stale.clear()
            self._ax_reprice.clear()
            self._ax_model.clear()

            # ── 成交价走势（折线）
            ax = self._ax_trend
            if recent:
                dates = [r.sold_time for r in recent]
                prices = [r.sold_price for r in recent]
                ax.plot(dates, prices, "o-", color="#4e79a7", markersize=3, linewidth=1.5)
                ax.set_title(f"成交价走势（近{days}天）", fontsize=10)
                ax.set_ylabel("价格（元）")
                ax.tick_params(axis="x", rotation=30, labelsize=8)
            else:
                ax.text(0.5, 0.5, "暂无数据", ha="center", va="center",
                        transform=ax.transAxes, color="gray")
                ax.set_title("成交价走势", fontsize=10)

            # ── 在架天数分布（直方图）
            ax = self._ax_stale
            from ..services.zhuanzhuan_api import ImeiService
            # 复用 batch_store 里的数据（如果有的话）
            if hasattr(self.ctx, "zhuanzhuan"):
                items = self.ctx.zhuanzhuan.batch_store.get_all()
                stale_days_list = [
                    i.stale_days for i in items
                    if i.stale_days is not None
                ]
                if stale_days_list:
                    ax.hist(stale_days_list, bins=10, color="#59a14f", edgecolor="white")
                    ax.set_title("在架天数分布", fontsize=10)
                    ax.set_xlabel("天数")
                    ax.set_ylabel("商品数")
                else:
                    ax.text(0.5, 0.5, "请先加载批量列表", ha="center", va="center",
                            transform=ax.transAxes, color="gray")
                    ax.set_title("在架天数分布", fontsize=10)
            else:
                ax.text(0.5, 0.5, "暂无数据", ha="center", va="center",
                        transform=ax.transAxes, color="gray")
                ax.set_title("在架天数分布", fontsize=10)

            # ── 每日改价次数（柱状图）
            ax = self._ax_reprice
            if daily_summary:
                dates_s  = [d["date"][5:] for d in daily_summary]  # MM-DD
                counts   = [d["count"] for d in daily_summary]
                colors   = ["#e15759" if d["avg_diff"] < 0 else "#59a14f"
                            for d in daily_summary]
                bars = ax.bar(dates_s, counts, color=colors, edgecolor="white")
                ax.set_title(f"每日改价次数（近{days}天）", fontsize=10)
                ax.set_ylabel("次数")
                ax.tick_params(axis="x", rotation=30, labelsize=8)
                # 数值标签
                for bar, cnt in zip(bars, counts):
                    ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.2,
                            str(cnt), ha="center", va="bottom", fontsize=8)
            else:
                ax.text(0.5, 0.5, "暂无改价记录", ha="center", va="center",
                        transform=ax.transAxes, color="gray")
                ax.set_title("每日改价次数", fontsize=10)

            # ── 型号改价排行（横向柱状图）
            ax = self._ax_model
            if model_summary:
                models = [m["model"][:10] for m in model_summary]
                counts_m = [m["count"] for m in model_summary]
                y_pos = range(len(models))
                ax.barh(list(y_pos), counts_m, color="#76b7b2", edgecolor="white")
                ax.set_yticks(list(y_pos))
                ax.set_yticklabels(models, fontsize=8)
                ax.set_title(f"型号改价TOP{len(models)}（近{days}天）", fontsize=10)
                ax.set_xlabel("改价次数")
            else:
                ax.text(0.5, 0.5, "暂无数据", ha="center", va="center",
                        transform=ax.transAxes, color="gray")
                ax.set_title("型号改价排行", fontsize=10)

            for a in [self._ax_trend, self._ax_stale, self._ax_reprice, self._ax_model]:
                a.set_facecolor("#fafafa")
                a.spines["top"].set_visible(False)
                a.spines["right"].set_visible(False)

            self._fig.tight_layout()
            self._canvas.draw()

        self.frame.after(0, draw)
