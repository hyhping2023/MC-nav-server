#!/usr/bin/env bash
# 下载 Purpur 1.20.1 latest -> server/purpur.jar（幂等：已存在则跳过）
set -euo pipefail
cd "$(dirname "$0")"

JAR="purpur.jar"
URL="https://api.purpurmc.org/v2/purpur/1.20.1/latest/download"

if [ -s "$JAR" ]; then
  echo "[download.sh] $JAR already exists ($(ls -lh "$JAR" | awk '{print $5}')), skipping."
  exit 0
fi

echo "[download.sh] Downloading Purpur 1.20.1 latest -> $JAR"
curl -fL -o "$JAR" "$URL"
echo "[download.sh] Done: $(ls -lh "$JAR" | awk '{print $9, $5}')"
