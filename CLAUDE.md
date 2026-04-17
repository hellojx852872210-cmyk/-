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

## 执行要求
- 从本次起，以上“分支规范 / Commit 规范 / 每日开发 Checklist”作为本项目默认执行规则，后续协作严格遵循。
