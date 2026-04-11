# 转转多浏览器隔离会话设计

## 背景

Qt 数据管理页原先只绑定固定浏览器会话 `zhuanzhuan-fetch`。底层其实已经通过 `QWebEngineProfile` + 独立 storage/cache 目录支持持久化与隔离，但 UI 缺少“浏览器实例管理”能力，因此多个店铺无法稳定复用各自独立的登录态。

## 本次方案

### 1. 引入浏览器实例元数据存储

新增 `BrowserInstanceStore`，将浏览器实例信息持久化到 `config/browser_instances.json`：

- `instance_id`
- `platform`
- `name`
- `profile_key`
- `is_default`
- `created_at`
- `updated_at`

实例 ID 基于名称 slug 生成，并在同平台内自动去重。`profile_key` 稳定映射为：

- `zhuanzhuan-fetch-<instance_id>`

这样每个浏览器实例始终对应固定 profile 目录，重启后仍能恢复各自 Cookie / LocalStorage / Session。

### 2. 保持现有浏览器会话体系兼容

`BrowserSessionRegistry` 继续负责按 `profile_key` 懒加载会话。

`PlatformBrowserContext` 在保留静态入口：

- `fetch`
- `imei`
- `batch`

的同时，新增动态按实例取会话的能力。这样可以让 `fetch` 页先切换到多实例模式，而不强迫其它子页一起改造。

### 3. 数据管理页改为“多浏览器实例管理”

`ui_qt/tab_fetch.py` 现在提供：

- 浏览器实例列表
- 新建浏览器
- 打开浏览器
- 重命名
- 删除
- 设为默认
- 展示实例名 / 实例 ID / Profile Key / Profile Dir

页面打开浏览器时，不再使用固定 `zhuanzhuan-fetch`，而是使用当前选中实例的 `profile_key` 获取会话。

### 4. 浏览器隔离边界

本次在“浏览器实例隔离持久化”之外，补充了一个轻量的 Cookie 提取能力，用于把当前浏览器实例中的登录 Cookie 手动回填到账号配置。

- 浏览器窗口仍然基于各自独立的 `QWebEngineProfile` 持久化
- “获取 Cookie”会优先读取运行时 `cookieStore`
- 若运行时未返回结果，则回退读取该 profile 目录下 `storage/Cookies` SQLite
- 导出的 Cookie 会显示在页面文本框中，并允许手动删改后再回填到账号
- “回填到账号”仍然是手动动作，不做浏览器 Cookie 到账号配置的自动同步

这样既保持浏览器登录态与账号文本 Cookie 的边界清晰，又能满足实际业务里“从浏览器会话取 Cookie 用于接口调用”的需求。

## 关键文件

- `zhuanzhuan_pricing/services/browser_instance_store.py`
- `zhuanzhuan_pricing/browser/session.py`
- `zhuanzhuan_pricing/ui/app.py`
- `zhuanzhuan_pricing/ui_qt/tab_fetch.py`
- `zhuanzhuan_pricing/config.py`

## 验证建议

1. 启动 Qt 主程序，进入“转转 → 数据管理”。
2. 新建两个浏览器实例，例如“店铺A”“店铺B”。
3. 分别打开两个浏览器窗口，登录不同账号。
4. 关闭应用并重新启动。
5. 再次打开两个实例，确认登录态仍分别保留。
6. 检查显示的 `Profile Dir` 是否对应不同目录。
7. 验证 IMEI / 批量处理等其它页面仍能正常初始化。

---

# 低于成本价改成人工确认

## 背景

本次把定价策略调整为“周转优先”。允许建议价低于成本价，但自动流程不能直接执行，必须进入人工确认。

核心区别：

- `market_floor` 仍然作为自动定价的市场保护线
- `cost_floor` 不再作为自动执行时的硬钳制下限
- 低于成本的候选价会保留，但会被显式标记为 `需人工确认`

## 本次改动

### 1. 决策层拆分市场底线和成本底线

`automation/tasks.py` 中的 `build_reprice_decision(...)` 现在：

- 仍用 `market_floor` / `market_cap` 做价格钳制
- 不再把 `cost_floor` 混进自动钳制逻辑
- 在得到最终候选价后，通过 `_assess_manual_review(...)` 判断：
  - 是否低于成本
  - 是否低于目标利润
  - 是否需要人工确认
  - 预计亏损多少
  - 与成本底线差多少

新增的决策结果字段包括：

- `below_cost`
- `below_target_profit`
- `needs_manual_review`
- `manual_review_reason`
- `loss_amount`
- `target_profit_gap`
- `price_gap_to_cost_floor`

### 2. 自动任务遇到低于成本时不再自动执行

以下自动流程都已接入人工确认门槛：

- `task_auto_reprice(...)`
- `task_stale_drop(...)`
- `task_auto_list(...)`

当 `decision["needs_manual_review"] == True` 时：

- 不执行自动改价 / 自动降价 / 自动上架接口
- 保留 `suggested_price` 和 `new_price` 作为候选执行价
- 将商品状态写成：
  - `op_status = "待确认"`
  - `reprice_ok = None`
- 在 `reprice_msg` / `op_message` 里写入原因和预览信息，供人工复核

### 3. 预览文案补充人工确认提示

`_pricing_preview(...)` 现在会在预览里显示：

- `需人工确认`
- `预计亏损 xxx`
- 低于成本的具体原因文案

这样 UI 不需要新增一套状态机制，也能直接复用现有字段展示人工确认信号。

### 4. 规则引擎不再把成本底线当成绝对下限

`core/pricing_engine.py` 中规则应用的 floor guard 已调整为只参考市场底价：

- 之前：可能把 `cost_floor_price` 当成硬下限
- 现在：只用 `market_floor_price or floor_price`

这样规则命中后，仍可产生低于成本的候选价，但不会绕过人工确认机制。

## 关键文件

- `zhuanzhuan_pricing/automation/tasks.py`
- `zhuanzhuan_pricing/core/pricing_engine.py`
- `zhuanzhuan_pricing/test_pricing_manual_review.py`

## 最小验证

新增单测：`zhuanzhuan_pricing/test_pricing_manual_review.py`

覆盖点：

1. 当候选价低于成本目标线时，不会被抬回 `cost_floor`
2. 仍然会遵守 `market_floor`
3. 返回结果会带上：
   - `below_cost = True`
   - `needs_manual_review = True`
   - `manual_review_reason` 包含“需人工确认”

本次实际验证命令：

```bash
python3 -m py_compile automation/tasks.py core/pricing_engine.py test_pricing_manual_review.py
cd /Users/shyn/Desktop/project2 && python3 -m unittest zhuanzhuan_pricing.test_pricing_manual_review
```

结果：通过。

## 提交记录

本次代码已提交：

- commit: `25f2d32`
- message: `feat: gate below-cost pricing behind manual review`
