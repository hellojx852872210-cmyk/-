#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="/Users/shyn/Desktop/project2"
PYTHON_BIN="python3"

cd "$PROJECT_ROOT"

echo "[Agent] 启动前配置"
echo ""

echo "[1/6] 是否新增/更新店铺账号？"
echo "  1) 是，交互添加"
echo "  2) 否，继续"
read "ADD_ACCOUNT_CHOICE?请输入选项 [1/2，默认2]: "
ADD_ACCOUNT_CHOICE="${ADD_ACCOUNT_CHOICE:-2}"
if [[ "$ADD_ACCOUNT_CHOICE" == "1" ]]; then
  echo ""
  echo "[Agent] 进入账号交互添加..."
  if ! "$PYTHON_BIN" -m zhuanzhuan_pricing.automation.agent_runner config add-account-interactive; then
    echo ""
    echo "[Agent] 账号添加失败，返回启动配置继续。"
  else
    echo ""
    echo "[Agent] 当前账号列表："
    "$PYTHON_BIN" -m zhuanzhuan_pricing.automation.agent_runner config list-accounts || true
  fi
fi

echo ""
echo "[2/6] 是否再添加一个账号？"
echo "  1) 是，再添加一个"
echo "  2) 否"
read "ADD_ACCOUNT_CHOICE_2?请输入选项 [1/2，默认2]: "
ADD_ACCOUNT_CHOICE_2="${ADD_ACCOUNT_CHOICE_2:-2}"
if [[ "$ADD_ACCOUNT_CHOICE_2" == "1" ]]; then
  echo ""
  echo "[Agent] 进入账号交互添加..."
  if ! "$PYTHON_BIN" -m zhuanzhuan_pricing.automation.agent_runner config add-account-interactive; then
    echo ""
    echo "[Agent] 账号添加失败，返回启动配置继续。"
  fi
fi

echo ""
echo "[3/6] 选择任务组合："
echo "  1) erp_sync + auto_reprice（推荐）"
echo "  2) 仅 auto_reprice"
echo "  3) erp_sync + auto_reprice + stale_drop"
read "TASK_CHOICE?请输入选项 [1/2/3，默认1]: "
TASK_CHOICE="${TASK_CHOICE:-1}"
case "$TASK_CHOICE" in
  2) TASKS="auto_reprice" ;;
  3) TASKS="erp_sync,auto_reprice,stale_drop" ;;
  *) TASKS="erp_sync,auto_reprice" ;;
esac

echo ""
echo "[4/6] 设置轮询间隔（秒）"
read "INTERVAL_SECONDS?请输入间隔 [默认600]: "
INTERVAL_SECONDS="${INTERVAL_SECONDS:-600}"
if [[ ! "$INTERVAL_SECONDS" =~ '^[0-9]+$' ]] || [[ "$INTERVAL_SECONDS" -lt 60 ]]; then
  echo "[Agent] 间隔无效，自动使用默认 600 秒"
  INTERVAL_SECONDS=600
fi

echo ""
echo "[5/6] 待确认处理策略："
echo "  1) 启动前自动 reject_and_ignore（无人值守推荐）"
echo "  2) 跳过处理（保留待确认）"
read "REVIEW_CHOICE?请输入选项 [1/2，默认1]: "
REVIEW_CHOICE="${REVIEW_CHOICE:-1}"

echo ""
echo "[6/6] 是否先执行 cookie 校验："
echo "  1) 是（推荐）"
echo "  2) 否"
read "COOKIE_CHECK_CHOICE?请输入选项 [1/2，默认1]: "
COOKIE_CHECK_CHOICE="${COOKIE_CHECK_CHOICE:-1}"

echo ""
echo "[Agent] 配置确认："
echo "[Agent] tasks=$TASKS"
echo "[Agent] interval_seconds=$INTERVAL_SECONDS"
if [[ "$REVIEW_CHOICE" == "1" ]]; then
  echo "[Agent] manual_review=reject_and_ignore"
else
  echo "[Agent] manual_review=skip"
fi
if [[ "$COOKIE_CHECK_CHOICE" == "1" ]]; then
  echo "[Agent] cookie_check=on"
else
  echo "[Agent] cookie_check=off"
fi

echo ""
if [[ "$COOKIE_CHECK_CHOICE" == "1" ]]; then
  echo "[Agent] 预检：校验多店 cookies ..."
  if ! "$PYTHON_BIN" -m zhuanzhuan_pricing.automation.agent_runner config validate-cookies; then
    echo ""
    echo "[Agent] Cookie 校验失败，已停止启动。请先更新失效账号 cookie。"
    read -k 1 "?按任意键退出..."
    echo ""
    exit 1
  fi
fi

if [[ "$REVIEW_CHOICE" == "1" ]]; then
  echo ""
  echo "[Agent] 预处理：清理待确认（reject_and_ignore）..."
  if ! "$PYTHON_BIN" -m zhuanzhuan_pricing.automation.agent_runner config resolve-manual-review --mode reject_and_ignore; then
    echo ""
    echo "[Agent] 待确认预处理存在失败，请先检查账号状态或手动处理后再启动。"
    read -k 1 "?按任意键退出..."
    echo ""
    exit 1
  fi
fi

echo ""
echo "[Agent] 启动中..."
echo "[Agent] 项目目录: $PROJECT_ROOT"
echo ""

exec "$PYTHON_BIN" -m zhuanzhuan_pricing.automation.agent_runner run \
  --tasks "$TASKS" \
  --interval-seconds "$INTERVAL_SECONDS"
