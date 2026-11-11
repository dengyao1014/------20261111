#!/bin/bash
# 需求挖掘系统每日自动运行入口。
# 由 launchd 任务 com.demandminer.daily 每天北京时间 01:00 触发；
# 机器休眠时会顺延到唤醒后补跑。
#
# 手动测试：./run_daily.sh --dry-run
# 手动正式跑：./run_daily.sh

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export LANG="${LANG:-zh_CN.UTF-8}"

PY="$ROOT/.venv/bin/python"
LOG_DIR="$ROOT/data/logs"
LOCK_DIR="$ROOT/data/.daily-run.lock"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date +%Y-%m-%d).log"

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

# 防止上一轮没跑完时又启动一轮（PID 已不存在的锁视为残留）
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    owner="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null; then
        log "已有运行中的任务 (pid $owner)，本次跳过"
        exit 0
    fi
    log "清理残留锁 $LOCK_DIR"
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR" || exit 1
fi
echo $$ >"$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM

if [ ! -x "$PY" ]; then
    log "找不到虚拟环境解释器：$PY"
    exit 1
fi

# 清理 30 天前的日志
find "$LOG_DIR" -name '*.log' -type f -mtime +30 -delete 2>/dev/null || true

run_once() {
    log "开始运行：main.py $*"
    "$PY" main.py "$@" >>"$LOG_FILE" 2>&1
}

run_once "$@"
status=$?

# 失败后 10 分钟重试一次（多为临时网络问题）
if [ "$status" -ne 0 ]; then
    log "退出码 $status，10 分钟后重试一次"
    sleep 600
    run_once "$@"
    status=$?
fi

if [ "$status" -ne 0 ]; then
    log "最终失败（退出码 $status）"
    "$PY" -m demand_miner.notify "⚠️ 需求挖掘系统运行失败（退出码 $status）\n日志：$LOG_FILE" \
        >>"$LOG_FILE" 2>&1 || true
    osascript -e 'display notification "需求挖掘系统运行失败，请查看 data/logs" with title "需求挖掘系统"' \
        >/dev/null 2>&1 || true
    exit "$status"
fi

dry=0
for arg in "$@"; do
    [ "$arg" = "--dry-run" ] && dry=1
done
if [ "$dry" -eq 1 ]; then
    log "运行成功，dry-run 报告：$ROOT/out/dry-run-$(date +%Y-%m-%d).md"
else
    log "运行成功，报告：$ROOT/out/latest.md"
fi
exit 0
