# CLAUDE.md

## 项目当前目标
- 先完成仓库规范化（目录与忽略规则清晰），再进行功能修改。
- 将 MCP/工具节点接入说明统一到本文件，作为 Claude 协作入口。

## 仓库规范（Git 形式）
- 代码主目录：`zhuanzhuan_pricing/`
- 配置目录：`config/`
- 运行时产物目录：`runtime/`（不入库）
- 浏览器本地 profile：`data/browser_profiles/`（不入库）

> 已在 `.gitignore` 中明确忽略：`runtime/`、`data/browser_profiles/` 及敏感本地配置文件。

## MCP/工具节点接入清单

### 1) bridge-cli 节点（推荐默认节点）
- **节点名**：`bridge-cli`
- **入口**：`python3 -m zhuanzhuan_pricing.bridge_cli`
- **用途**：将 Claude B 回传块转发到 tmux pane
- **关键参数**：
  - `--target-pane <session:window.pane>`（必填）
  - `--source <path>`（默认 `runtime/claude_b_report.txt`）
  - `--state-file <path>`（默认 `runtime/claude_bridge_state.json`）
  - `--watch` / `--forward-once`
  - `--dry-run`（联调建议先开）

**示例（先 dry-run）**
```bash
python3 -m zhuanzhuan_pricing.bridge_cli \
  --target-pane mysession:0.1 \
  --forward-once \
  --dry-run
```

### 2) bridge-gui 节点
- **节点名**：`bridge-gui`
- **入口**：`python3 -m zhuanzhuan_pricing.bridge_gui`
- **用途**：图形化控制监听/单次转发，便于人工确认
- **适用场景**：本地调试、手动观测日志与结果

## 节点接入约束
- 默认先 `dry-run` 验证消息结构，再发送到目标 pane。
- 回传内容必须走脱敏流程（`zhuanzhuan_pricing/bridge/redaction.py`）。
- `--watch` 与 `--forward-once` 不能同时使用。
- 状态文件写入 `runtime/`，不提交到仓库。

## 联调检查清单
1. `runtime/claude_b_report.txt` 存在且包含 `【Claude B 回传】` 块。
2. tmux target pane 可用（如 `session:0.1`）。
3. 单次转发检查：
   - `python3 -m zhuanzhuan_pricing.bridge_cli --target-pane <pane> --forward-once --dry-run`
4. 监听检查：
   - `python3 -m zhuanzhuan_pricing.bridge_cli --target-pane <pane> --watch`
5. GUI 检查：
   - `python3 -m zhuanzhuan_pricing.bridge_gui`

## 相关代码位置
- `zhuanzhuan_pricing/bridge_cli.py`
- `zhuanzhuan_pricing/bridge/report_bridge.py`
- `zhuanzhuan_pricing/bridge_gui.py`
- `zhuanzhuan_pricing/ui_qt/main_window.py`

## 分支规范（严格执行）
- 采用“一需求一分支”：一个分支只做一件事。
- 命名规则：
  - 新功能：`feature/<short-topic>`
  - 缺陷修复：`fix/<short-topic>`
  - 重构整理：`refactor/<short-topic>`
- 禁止在同一分支混入无关改动（例如：功能 + 大量格式化 + 文件搬迁）。
- 开发前先同步主线，再从最新主线切分支。

## Commit 规范（严格执行）
- 单个 commit 保持“单一目的、可独立回滚”。
- 提交类型建议：
  - `feat:` 新功能
  - `fix:` 缺陷修复
  - `refactor:` 纯重构（不改外部行为）
  - `test:` 测试新增/修正
  - `docs:` 文档修改
  - `chore:` 工程与配置调整
- 提交消息模板：
  - 标题：`<type>: <变化目的>`
  - 正文（可选）：
    - Why: 为什么改
    - What: 做了什么（关键点）
    - Verify: 如何验证
- 禁止一次 commit 混入多类目标（例如同时提交功能与大规模目录清理）。

## 每日开发 Checklist（严格执行）
1. 开始前
   - 阅读本文件与当前任务范围，确认目标边界。
   - `git status` 确认工作区干净或仅有预期改动。
2. 开发中
   - 先最小改动实现，再补最小必要测试。
   - 节点链路先 `--dry-run`，通过后再真实转发。
3. 提交前
   - Python 改动执行：`python3 -m py_compile <touched files>`。
   - 运行受影响测试（最小集合优先）。
   - 再次 `git status`，确认无运行时/敏感文件入库。
4. 收尾
   - 用规范 commit message 提交。
   - 记录本次变更范围与验证结果，便于下一轮接续。

## 定价自动化阶段性节点（灰度上线）
- **Phase A（自动改价）**：仅开启 `task_auto_reprice`，先单账号+小批量运行。
- **Phase B（滞销降价）**：在 A 稳定后开启 `task_stale_drop`，继续小流量放量。
- **Phase C（自动上架）**：在 A/B 稳定后最后开启 `task_auto_list`。

### 每阶段放量前必须满足
- 最近观察窗口内失败率稳定，无持续上升趋势。
- `待确认` 占比可控，人工队列可在当日处理。
- 无异常跳价（大幅偏离市场基准或成本目标）的集中出现。
- 外部接口错误（ERP/转转）处于可接受范围且可解释。

### 回滚/降级触发
- 失败率或接口报错率连续异常上升。
- 出现批量异常跳价或人工确认堆积。
- 立即降回上一阶段，仅保留已验证稳定的任务。

## 执行要求
- 从本次起，以上“分支规范 / Commit 规范 / 每日开发 Checklist”作为本项目默认执行规则，后续协作严格遵循。

## UI 迭代节点（2026-04-19）
- Qt 自动化「商品管理（ERP 导入）」已支持按商品状态筛选（全部/在售/未上架/已售/已下架/质检中/未知）。
- 「刷新导入状态」增加即时反馈（刷新中提示、按钮短暂禁用、防重入、最后刷新时间显示）。
- 本节点仅涉及 UI 侧最小改动：`zhuanzhuan_pricing/ui_qt/tab_auto.py`；未改任务编排与数据层。
- 商品管理表格新增“成本价”列，直接显示导入商品 `cost_price`，用于与 ERP 成本核对。
- ERP 成本提取口径对齐 ERP 原值：优先 `real_cost_price`，回退 `cost_price`、`cost`，不再额外加税。

## 官方建议价接入节点（2026-04-22）
- 官方建议价接口已接入自动化定价主链，采用“样本不足 fallback + 样本充足偏离对照”的策略。

### 1) API 入口（转转官方建议价）
- `zhuanzhuan_pricing/services/zhuanzhuan_api.py`
  - `ImeiService.query_official_reference_price(...)`
  - 对接接口：`queryDoubleGradePurchasePrice`

### 2) automation 注入入口
- `zhuanzhuan_pricing/automation/tasks.py`
  - `_fetch_official_reference_for_item(...)`
  - 在三条任务链路注入 `official_reference_fetcher`：
    - `task_auto_reprice`
    - `task_stale_drop`
    - `task_auto_list`

### 3) 定价流水线入口
- `zhuanzhuan_pricing/services/reprice_service.py`
  - `run_reprice_pipeline(..., official_reference_fetcher=...)`
  - `build_reprice_decision(..., official_reference=...)`

### 4) 决策输出与风控信号
- `build_reprice_decision` 输出新增官方信号字段：
  - `official_reference_price`
  - `official_reference_settle_price`
  - `official_reference_grade_name`
  - `official_reference_sku_id`
  - `official_reference_used_as_anchor`
  - `official_deviation_pct` / `official_deviation_abs`
  - `official_risk_triggered` / `official_risk_reason`
- 与现有成本/利润守卫并集触发手动确认，不改变既有手动确认闭环。

### 5) UI 预览节点
- `zhuanzhuan_pricing/ui_qt/tab_auto.py`
  - 自动化预览区新增官方参考信息展示：官方参考价、相对官方偏离、是否触发官方风控。

## Agent 使用说明（2026-05-09）

### 入口
- 一键启动脚本：`启动改价Agent.command`
- 命令行入口：`python3 -m zhuanzhuan_pricing.automation.agent_runner`

### 启动前交互配置
`启动改价Agent.command` 启动时会依次询问：
1. 是否新增/更新店铺账号（交互输入 name/note/cookie）
2. 是否再添加一个账号
3. 任务组合（`erp_sync` / `auto_reprice` / `stale_drop`）
4. 轮询间隔秒数
5. 待确认处理策略
6. 是否执行 cookie 校验

### 运行日志与表格
- 每轮结束后输出两张表：
  - 任务汇总表：`Task | Total | OK | Skip | Fail | ManualReview | Persisted | Note`
  - 改价记录样例表：`Task | Account | Item | Old | New | Diff | Trigger`
- 若本轮无改价写入，会提示：`Cycle repricing records: no persisted price changes`
- 每轮结束后会打印等待信息：`Cycle N idle: waiting Xs, next cycle at HH:MM:SS`

### 待确认处理策略
- 支持命令：
  - `python3 -m zhuanzhuan_pricing.automation.agent_runner config resolve-manual-review --mode reject_and_ignore`
- Agent 每轮结束后会自动执行一次待确认处理，避免无人值守时堆积。

### 跨电脑使用
1. 在新电脑 clone 仓库
2. 安装 Python 依赖
3. 运行 `启动改价Agent.command`，在本机重新录入账号 cookie
4. 建议先用较短间隔小流量观察，再逐步放量
