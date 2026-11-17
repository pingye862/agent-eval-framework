#!/bin/sh
# 启动 W03 被测服务（前台运行，Ctrl-C 结束）
#
# 用法：
#   export FDE_MODEL_URL=... FDE_MODEL_NAME=... FDE_MODEL_KEY=...
#   ./start_service.sh
#
# 可选环境变量：
#   W03_PROJECT  工程根目录（默认 ~/Downloads/W03/工程/fde-student-w03/fde-agent-w03）
#   W03_HOME     product home（默认 $W03_PROJECT/.runtime/product）
#   W03_PORT     监听端口（默认 8765，与 run_eval.py 的 --base-url 对应）
#
# 为什么要在同一个 shell 里配：product.py 启动时 construct CompatibleModel，
# 构造函数读 os.environ，缺任一项直接 SystemExit(MODEL_NOT_CONFIGURED)。

set -e

# 在 WorkBuddy 等宿主环境里启动时，PYTHONPATH 会注入一个 sitecustomize 补丁，
# 它把 mkdir 劫持成「目录已存在也抛 FileExistsError」，导致服务每条请求都崩。
# 这里显式摘掉，保证用纯净的 venv Python 启动。
unset PYTHONPATH PYTHONSTARTUP

PROJECT="${W03_PROJECT:-$HOME/Downloads/W03/工程/fde-student-w03/fde-agent-w03}"
HOME_DIR="${W03_HOME:-$PROJECT/.runtime/product}"
PORT="${W03_PORT:-8765}"

missing=""
for v in FDE_MODEL_URL FDE_MODEL_NAME FDE_MODEL_KEY; do
    eval "val=\$$v"
    [ -n "$val" ] || missing="$missing $v"
done

if [ -n "$missing" ]; then
    echo "ERROR: missing env:$missing"
    echo "  export FDE_MODEL_URL=\"https://<endpoint>/v1/chat/completions\""
    echo "  export FDE_MODEL_NAME=\"<model>\""
    echo "  export FDE_MODEL_KEY=\"sk-...\""
    echo "  (course gateway: export FDE_MODEL_PROFILE=gateway-low)"
    exit 2
fi

if [ ! -x "$PROJECT/.venv/bin/python" ]; then
    echo "ERROR: $PROJECT/.venv/bin/python not found"
    echo "  see 配置指南.md section 1 (uv venv --python 3.11 .venv)"
    exit 2
fi

if [ ! -f "$HOME_DIR/metrics-binding.json" ]; then
    echo "WARN: $HOME_DIR/metrics-binding.json not found"
    echo "  指标库未加载/未 activate，服务能起但查数会走「未绑定指标库」分支"
    echo "  先跑: .venv/bin/python tools/w03.py load --library examples/w03/library \\"
    echo "        --data examples/w03/data --project .runtime/wutong-v3 --activate-home $HOME_DIR"
fi

# 只打印端点主机，避免密钥进日志
echo "model : $FDE_MODEL_NAME @ $(echo "$FDE_MODEL_URL" | sed 's#\(://[^/]*\)/.*#\1#')"
echo "home  : $HOME_DIR"
echo "listen: http://127.0.0.1:$PORT"
echo "---"

cd "$PROJECT"
exec .venv/bin/python tools/product.py start --home "$HOME_DIR" --port "$PORT"
