#!/usr/bin/env bash
# 从 proto/vla.proto 生成 Python gRPC 代码到 vla_env/vla_env/proto/。
# 依赖：grpcio-tools（pip install grpcio-tools）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROTO_DIR="$SCRIPT_DIR/../proto"
OUT_DIR="$SCRIPT_DIR/../vla_env/proto"

mkdir -p "$OUT_DIR"

python -m grpc_tools.protoc \
  -I "$PROTO_DIR" \
  --python_out="$OUT_DIR" \
  --grpc_python_out="$OUT_DIR" \
  "$PROTO_DIR/vla.proto"

# grpcio-tools 生成的 vla_pb2_grpc.py 使用顶层 import（import vla_pb2），
# 在 vla_env.proto 子包内无法解析，需改为相对导入。
GRPC_FILE="$OUT_DIR/vla_pb2_grpc.py"
if [ -f "$GRPC_FILE" ]; then
  python - "$GRPC_FILE" <<'PY'
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    src = fh.read()
fixed = src.replace("import vla_pb2 as vla__pb2", "from . import vla_pb2 as vla__pb2")
if fixed != src:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(fixed)
    print("patched: vla_pb2_grpc.py 改用相对导入")
PY
fi

# 让 proto 目录成为可导入的 Python 包（vla_env.proto 子包）。
if [ ! -f "$OUT_DIR/__init__.py" ]; then
  echo '"""Generated gRPC code for vla.proto (vla_env.proto 子包)."""' > "$OUT_DIR/__init__.py"
fi

echo "Generated files:"
ls -la "$OUT_DIR"
