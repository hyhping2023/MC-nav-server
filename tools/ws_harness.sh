#!/usr/bin/env bash
# M1 WS harness：独立启动 WsServer 独立测试入口（不加载 Minecraft）。
# 用法：bash tools/ws_harness.sh [port]    （默认 30001）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-30001}"

JAR="$ROOT/fabric-vla-client/build/libs/vla-client-0.1.0.jar"
if [ ! -f "$JAR" ]; then
    echo "ERROR: missing $JAR (run: cd fabric-vla-client && ./gradlew build)" >&2
    exit 1
fi

# Java-WebSocket jar（Gradle 缓存）
JWS="$(find "$HOME/.gradle/caches/modules-2/files-2.1/org.java-websocket/Java-WebSocket/1.5.6/" -name '*.jar' ! -name '*sources*' 2>/dev/null | head -n1 || true)"
if [ -z "$JWS" ]; then
    echo "ERROR: Java-WebSocket 1.5.6 jar not found in Gradle cache" >&2
    exit 1
fi

# Gson jar：优先 tools/ws_harness/libs，缺失则下载
GSON_DIR="$ROOT/tools/ws_harness/libs"
GSON="$GSON_DIR/gson-2.10.1.jar"
if [ ! -f "$GSON" ]; then
    mkdir -p "$GSON_DIR"
    echo "Downloading gson-2.10.1.jar -> $GSON" >&2
    curl -fL --retry 3 -o "$GSON" \
        "https://repo1.maven.org/maven2/com/google/code/gson/gson/2.10.1/gson-2.10.1.jar" \
        || GSON="$(find "$HOME/.gradle/caches/modules-2/files-2.1/com.google.code.gson/gson/2.10.1/" -name '*.jar' ! -name '*sources*' 2>/dev/null | head -n1 || true)"
fi
if [ -z "$GSON" ] || [ ! -f "$GSON" ]; then
    echo "ERROR: gson-2.10.1.jar unavailable (download failed & not in Gradle cache)" >&2
    exit 1
fi

# Java-WebSocket 运行期需要 slf4j-api（仅打印告警，无 binding 也可用）
SLF4J="$(find "$HOME/.gradle/caches/modules-2/files-2.1/org.slf4j/slf4j-api/" -name '*.jar' ! -name '*sources*' 2>/dev/null | head -n1 || true)"

CP="$JAR:$JWS:$GSON${SLF4J:+:$SLF4J}"
exec java -cp "$CP" dev.vla.client.net.WsServer "$PORT"
