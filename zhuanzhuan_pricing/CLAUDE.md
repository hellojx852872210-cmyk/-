# zhuanzhuan_pricing

## 1. Repo rules
- 浏览器会话沿用现有 `QWebEngineProfile` / profile 目录方案；不要新增并行持久化路径。
- 可跨重启的 UI 状态放 store/service，不放 tab 自定义文件。
- 保持现有浏览器入口兼容：`fetch` / `imei` / `batch`。
- 顶层 tab 初始化失败时，应降级为错误面板，不阻塞整个窗口。
- 改 Python 后执行：`python3 -m py_compile <touched files>`。
- git 暂存按文件名显式添加。

## 2. Scoped workflow
- 开始先报：模块范围 + 目标文件。
- 先分析，后实现。
- 默认只读必要文件。
- 默认不跨模块搜索；必须跨时先说明原因。
- 默认输出：`涉及文件` / `最小计划` / `风险` / `handoff`。
- 非明确要求，不改 `ui/app.py`。

## 3. Project map
### core
- 职责：规则、定价、模型。
- 入口：`core/rule_engine.py` / `core/pricing_engine.py` / `core/models.py`
- 边界：保持纯业务；不放 UI、调度、外部接口。

### services
- 职责：外部系统、store、统一流水线。
- 入口：`services/reprice_service.py` / `services/data_store.py` / `services/browser_instance_store.py` / `services/account_manager.py`
- 边界：不带 UI 控件状态；规则计算放 `core`。

### ui
- 职责：多平台桌面 UI。
- 入口：`ui/app.py` / `ui/tab_zhuanzhuan.py` / `ui/tab_paipai.py` / `ui/tab_xianyu.py` / `ui/tab_95fen.py`
- 边界：从 `AppContext` 取依赖；不在 UI 里发明持久化；自动化放 `automation`。

### automation
- 职责：调度与任务编排。
- 入口：`automation/scheduler.py` / `automation/tasks.py`
- 边界：只组合 `core` + `services`；不承载 UI 状态。

## 4. Wiring facts
- `ui/app.py` 负责顶层装配；`AppContext` 集中创建 store、引擎、浏览器会话、调度器。
- `ZhuanzhuanContext` 只暴露转转子模块需要的能力，是 UI 子模块边界基准。
- 价格策略相关改动优先落 `services/reprice_service.py`。
- 自动化负责编排，不直接承载 UI 状态。

## 5. Stable project constraints
- 浏览器实例隔离依赖 `services/browser_instance_store.py` + profile key 持久化；不要绕开这条链路另建浏览器状态方案。
- 低于成本价允许生成候选价，但必须走人工确认；自动流程不能直接执行。
- 价格钳制优先区分市场底线与人工确认门槛，不要把成本底线重新塞回自动硬钳制。
- ERP/批量匹配问题优先先查 `services/zhuanzhuan_api.py` 的批量查询 schema，再考虑 UI 或 ERP 入口。
- Qt 版“数据管理”页（`ui_qt/tab_fetch.py`）内容较长，外层必须使用可滚动容器（`QScrollArea` + `setWidgetResizable(True)`）承载，避免小屏或窗口高度不足时底部内容被裁切。

## 6. Default session slices
- A: `core/rule_engine.py` + `services/reprice_service.py`
- B: `ui/tab_zhuanzhuan.py` + `ui/windows/*`
- C: `automation/tasks.py` + `automation/scheduler.py`
- D: `services/*store*.py` + `services/account_manager.py`

## 7. Prompt templates
### 分析
```text
模块范围：<ui | services | core | automation>
目标文件：<1-3 个文件>
任务：只分析，不修改。
约束：只读目标文件；不扫描 `.venv` / `.idea`；输出：涉及文件 / 最小计划 / 风险 / handoff。
```

### 最小实现
```text
模块范围：<ui | services | core | automation>
目标文件：<精确文件列表>
任务：先分析，再做最小修改。
约束：默认不跨模块搜索；只补读直接依赖；非明确要求，不改 `ui/app.py`。
输出：涉及文件 / 最小计划 / 风险 / handoff。
```

### Bug 修复
```text
模块范围：services
目标文件：services/reprice_service.py
问题：<一个明确 bug>
要求：先找根因，再修改；默认只看 `services/reprice_service.py`；必要时才补 `core/pricing_engine.py` / `core/rule_engine.py`。
输出：根因 / 修改点 / 风险 / handoff。
```

### Handoff
```text
模块范围：<当前模块>
本轮只处理：<文件列表>
请继续，不要重跑全项目搜索。
已知上下文：入口文件 / 直接联动 / 未动边界。
请输出：涉及文件 / 最小计划 / 风险 / handoff。
```

## 8. UI 迭代节点（2026-04-19）
- Qt 自动化「商品管理（ERP 导入）」已支持按商品状态筛选（全部/在售/未上架/已售/已下架/质检中/未知）。
- 「刷新导入状态」增加即时反馈（刷新中提示、按钮短暂禁用、防重入、最后刷新时间显示）。
- 本节点仅涉及 UI 侧最小改动：`ui_qt/tab_auto.py`；未改任务编排与数据层。
- 商品管理表格新增“成本价”列，直接显示导入商品 `cost_price`，用于与 ERP 成本核对。
- ERP 成本提取口径对齐 ERP 原值：优先 `real_cost_price`，回退 `cost_price`、`cost`，不再额外加税。

## 9. 自动化/ERP 节点（2026-04-20）
- `task_erp_sync` 增加可观测性回归覆盖：验证最终 `done` 进度事件必达，且完成文案包含“同步成本价”。
- 增加并行拉取容错回归：在售/在库任一路拉取失败时，保留另一路成功结果并在 `summary/error` 显式暴露 ERP 拉取异常。
- 增加 fallback 预算回归：`_match_detail_for_erp_item` 在兜底预算耗尽时立即返回“预算已耗尽”，避免无界单条查询拖慢导入。
- 受影响回归已通过：`python3 -m unittest zhuanzhuan_pricing.test_pricing_manual_review`（25 tests, OK）。

## 10. ERP 导入提速与刷新可观测性节点（2026-04-20）
- `task_erp_sync` 采用在售/在库双路并行拉取，单路失败不阻断整体导入；`summary/error` 合并呈现异常来源。
- 导入进度事件标准化为 `fetch/match/write/done` 四阶段，Qt 自动化页进度条按 `stage/current/total/message` 实时展示。
- 账号匹配保留批量查码主链路，并新增兜底预算上限，预算耗尽后立即短路返回，避免导入阶段被单条查询拖慢。
- 新增导入商品定时实时刷新链路：后台周期刷新 `status/current_price/settle_price/listed_time`，并与手动刷新/ERP 导入防重入互斥。
- 受影响代码路径：`automation/tasks.py`、`services/erp_service.py`、`ui_qt/tab_auto.py`、`test_pricing_manual_review.py`。
