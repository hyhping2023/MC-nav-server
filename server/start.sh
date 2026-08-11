#!/usr/bin/env bash
# 启动 Purpur 服务端（JDK 21 + FIFO 命名管道控制台输入）
#
# 用法：
#   bash start.sh
#   VLA_SERVER_PIPE=/tmp/my_pipe bash start.sh   # 自定义管道路径
#   VLA_SERVER_XMS=1G VLA_SERVER_XMX=4G bash start.sh  # Java heap 上限
#
# 服务端控制台 stdin 来自 FIFO 命名管道（默认 /tmp/vla_server_console），
# 外部脚本可用 `echo "<command>" > "$PIPE"` 发送命令（见 freeze_gamerules.sh）。
set -euo pipefail
cd "$(dirname "$0")"

# JDK 21（Purpur 1.20.1 需要 Java 17+）
export JAVA_HOME="${JAVA_HOME:-/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home}"
JAVA="$JAVA_HOME/bin/java"
if [ ! -x "$JAVA" ]; then
  echo "[start.sh] ERROR: java not found at $JAVA" >&2
  exit 1
fi

PIPE="${VLA_SERVER_PIPE:-/tmp/vla_server_console}"
XMS="${VLA_SERVER_XMS:-1G}"
XMX="${VLA_SERVER_XMX:-4G}"

# FIFO 不存在则创建；已存在（管道或普通文件残留）则清理重建
if [ ! -p "$PIPE" ]; then
  if [ -e "$PIPE" ]; then
    echo "[start.sh] $PIPE exists but is not a FIFO, removing"
    rm -f "$PIPE"
  fi
  mkfifo "$PIPE"
  echo "[start.sh] created console pipe: $PIPE"
else
  echo "[start.sh] using existing console pipe: $PIPE"
fi

mkdir -p logs
LOG="logs/latest.log"

echo "[start.sh] JAVA=$JAVA"
echo "[start.sh] starting Purpur 1.20.1 (Xms${XMS} Xmx${XMX}, nogui) ..."

# 用「读+写」方式持有 FIFO 文件描述符：
#  - 作为控制台 stdin，外部脚本 `echo "<cmd>" > "$PIPE"` 即送入服务端；
#  - 因本进程同时持有读端与写端，FIFO 永不到达 EOF，写入方不会因无读者而阻塞；
#  - 服务端退出后脚本随即退出，不会残留阻塞在 FIFO 上的 tail 进程
#    （tail -f 管道方案在服务端退出后 tail 仍阻塞读 FIFO，导致脚本悬挂并污染下次启动）。
exec 3<>"$PIPE"
"$JAVA" "-Xms${XMS}" "-Xmx${XMX}" -jar purpur.jar nogui <&3
EXIT=$?
echo "[start.sh] server stopped (exit=$EXIT)"
exit "$EXIT"
