#!/usr/bin/env bash
# 通过 FIFO 控制台向服务端发送 gamerule 冻结命令（每条间隔 ~0.3s 防止丢弃）
#
# 前提：服务端已通过 start.sh 启动且控制台 FIFO 存在。
# 管道路径可用 VLA_SERVER_PIPE 覆盖（与 start.sh 保持一致）。
set -uo pipefail
cd "$(dirname "$0")"

PIPE="${VLA_SERVER_PIPE:-/tmp/vla_server_console}"

if [ ! -p "$PIPE" ]; then
  echo "[freeze_gamerules] ERROR: console pipe $PIPE not found (server not started via start.sh?)" >&2
  exit 1
fi

send() {
  echo "[freeze_gamerules] > $*"
  echo "$*" > "$PIPE"
  sleep 0.3
}

send "gamerule doDaylightCycle false"
send "gamerule doWeatherCycle false"
send "gamerule doMobSpawning false"
send "gamerule mobGriefing false"
send "gamerule keepInventory true"
send "gamerule randomTickSpeed 0"
send "time set 6000"
send "save-all"

echo "[freeze_gamerules] done."
