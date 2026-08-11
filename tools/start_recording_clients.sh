#!/usr/bin/env bash
# 启动多个长期存活的 Fabric 录制客户端。
#
# 每个客户端：
#   - 使用独立 runDir，避免 autojoin/options/logs/锁文件互相覆盖；
#   - 使用独立离线玩家身份和 WS 端口；
#   - 只在本脚本启动时拉起一次，Python record_worker 会在同一连接上连续录制 episode。
#
# 用法（在仓库根目录）：
#   bash tools/start_recording_clients.sh --count 2
#   bash tools/start_recording_clients.sh --count 4 --start-index 0 \
#       --base-ws-port 30001 --server 127.0.0.1:25565 --client-xmx 1G
#
# 日志和 PID：
#   runtime/worker-00/client.log
#   runtime/worker-00/client.pid
#
# 停止：
#   kill "$(cat runtime/worker-00/client.pid)"
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLIENT_DIR="$ROOT/fabric-vla-client"
RUNTIME_ROOT="${VLA_RUNTIME_ROOT:-$ROOT/runtime}"

COUNT=1
START_INDEX=0
BASE_WS_PORT=30001
MC_SERVER="127.0.0.1:25565"
PLAYER_PREFIX="agent"
CLIENT_XMX="1G"

usage() {
  cat <<'EOF'
Usage: bash tools/start_recording_clients.sh [options]

  --count N             Number of clients to start (default: 1)
  --start-index N       First worker index (default: 0)
  --base-ws-port N      worker-00 WS port (default: 30001)
  --server HOST:PORT    Minecraft server address (default: 127.0.0.1:25565)
  --player-prefix TEXT  Offline player prefix (default: agent -> agent00)
  --client-xmx SIZE     Per-client Java heap, e.g. 1G (default: 1G)
  --runtime-root PATH   Runtime output root (default: <repo>/runtime)
EOF
}

while (($#)); do
  case "$1" in
    --count) COUNT="$2"; shift 2 ;;
    --start-index) START_INDEX="$2"; shift 2 ;;
    --base-ws-port) BASE_WS_PORT="$2"; shift 2 ;;
    --server) MC_SERVER="$2"; shift 2 ;;
    --player-prefix) PLAYER_PREFIX="$2"; shift 2 ;;
    --client-xmx) CLIENT_XMX="$2"; shift 2 ;;
    --runtime-root) RUNTIME_ROOT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "[start_recording_clients] unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if ! [[ "$COUNT" =~ ^[1-9][0-9]*$ ]] || ! [[ "$START_INDEX" =~ ^[0-9]+$ ]]; then
  echo "[start_recording_clients] --count must be positive and --start-index non-negative" >&2
  exit 2
fi
if ! [[ "$BASE_WS_PORT" =~ ^[0-9]+$ ]]; then
  echo "[start_recording_clients] --base-ws-port must be numeric" >&2
  exit 2
fi

mkdir -p "$RUNTIME_ROOT"

for ((offset = 0; offset < COUNT; offset++)); do
  index=$((START_INDEX + offset))
  worker_id=$(printf 'worker-%02d' "$index")
  player=$(printf '%s%02d' "$PLAYER_PREFIX" "$index")
  ws_port=$((BASE_WS_PORT + index))
  worker_dir="$RUNTIME_ROOT/$worker_id"
  run_dir="$worker_dir/client-run"
  pid_file="$worker_dir/client.pid"
  log_file="$worker_dir/client.log"

  mkdir -p "$run_dir"
  printf '%s\n%s\n' "$MC_SERVER" "$player" > "$run_dir/autojoin.txt"

  if [[ -s "$pid_file" ]]; then
    old_pid=$(cat "$pid_file" || true)
    if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
      echo "[start_recording_clients] $worker_id already running (pid=$old_pid), skip"
      continue
    fi
    rm -f "$pid_file"
  fi

  echo "[start_recording_clients] starting $worker_id player=$player ws=$ws_port runDir=$run_dir"
  (
    cd "$CLIENT_DIR"
    exec ./gradlew --no-daemon runClient \
      "-PvlaRunDir=$run_dir" \
      "-PvlaWsPort=$ws_port" \
      "-PvlaClientXmx=$CLIENT_XMX"
  ) >"$log_file" 2>&1 &
  echo $! > "$pid_file"
done

echo "[start_recording_clients] started $COUNT client(s); logs under $RUNTIME_ROOT"
