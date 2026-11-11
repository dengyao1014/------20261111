#!/bin/bash
# 安装（或重装）每天 01:00 自动运行的 launchd 任务。
# 卸载：launchctl bootout gui/$(id -u)/com.demandminer.daily && rm ~/Library/LaunchAgents/com.demandminer.daily.plist

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.demandminer.daily"
PLIST_SRC="$ROOT/deploy/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"

if [ ! -f "$PLIST_SRC" ]; then
    echo "缺少模板：$PLIST_SRC" >&2
    exit 1
fi

chmod +x "$ROOT/run_daily.sh" "$ROOT/install_daily_task.sh"
mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data/logs"

sed "s|__PROJECT_DIR__|$ROOT|g" "$PLIST_SRC" >"$PLIST_DST"
plutil -lint "$PLIST_DST"

# 已经加载过就先卸载，避免 bootstrap 报 "already loaded"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST_DST"

echo
echo "已安装：$PLIST_DST"
echo "计划：每天 01:00（系统时区 $(date +%Z)）"
echo "查看状态：launchctl print gui/$UID_NUM/$LABEL"
echo "日志：$ROOT/data/logs/"
echo "立即测试（dry-run，不调用 LLM）：$ROOT/run_daily.sh --dry-run"
