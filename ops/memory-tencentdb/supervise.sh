#!/usr/bin/env bash
# ============================================================
# memory-tencentdb 组件守护 —— 启动/检查/停止
#
#   bash ~/.hermes/memory-tencentdb/supervise.sh start|stop|status|restart
#
# 组件:
#   1. local-embed.py  (127.0.0.1:8421)  本地 bge-small-zh-v1.5, OpenAI 兼容 /v1/embeddings
#   2. Gateway         (127.0.0.1:8420)  memory-tencentdb 核心 (Node)
#
# ⚠️ provider 自带 GatewaySupervisor 会自动拉起 Gateway, 但它不管 embed
#    → embed 必须独立常驻, 否则 Gateway 开了 embedding 后会一直失败.
# 历史: 原来这里是 embedding-shim.py(OpenAI→MiniMax 翻译), 因 MiniMax 余额耗尽
#    已换成本地模型; shim 保留在目录里作回退参考, 不再被本脚本调用.
# ============================================================
set -uo pipefail

# Hermes' gateway process exports its own PYTHONPATH (the 3.14 site-packages
# of the Hermes runtime). Inherited by a 3.11 sidecar venv it shadows the
# venv's own packages: local-embed.py then imports the 3.14 huggingface_hub and
# dies with `cannot import name 'ResolvedRevision'`. The sidecars only depend on
# their own venv, so drop the parent's interpreter search path here.
unset PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONEXECUTABLE

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EMBED_LOG="$HERE/embed.log"
GW_LOG="$HERE/gateway.log"
PID_DIR="$HERE/pids"
mkdir -p "$PID_DIR"

EMBED_VENV_PY="$HERE/.venv/bin/python"
EMBED_MODEL="$HERE/models/bge-small-zh-v1.5"

EMBED_PORT=8421
GW_PORT=8420
LLM_PROXY_PORT=8422


_port_alive() {
  local port="$1"
  curl -sS -m 3 -o /dev/null "http://127.0.0.1:${port}/health" 2>/dev/null
}


_pid_of() {
  local name="$1" f="$PID_DIR/$name.pid"
  [[ -f "$f" ]] && cat "$f" || echo ""
}


start_embed() {
  if _port_alive "$EMBED_PORT"; then
    echo "  ✅ embed 已在运行 (127.0.0.1:$EMBED_PORT)"
    return 0
  fi
  if [[ ! -x "$EMBED_VENV_PY" ]]; then
    echo "  ❌ embed 缺 venv ($EMBED_VENV_PY) → 先建 venv 并装 sentence-transformers"
    return 1
  fi
  if [[ ! -d "$EMBED_MODEL" ]]; then
    echo "  ❌ embed 缺模型 ($EMBED_MODEL) → 先下载 bge-small-zh-v1.5"
    return 1
  fi
  LOCAL_EMBED_MODEL="$EMBED_MODEL" EMBED_SHIM_LOG="$EMBED_LOG" \
    nohup "$EMBED_VENV_PY" "$HERE/local-embed.py" >>"$EMBED_LOG" 2>&1 &
  echo $! > "$PID_DIR/embed.pid"
  for _ in $(seq 1 15); do
    sleep 1
    if _port_alive "$EMBED_PORT"; then
      echo "  ✅ embed 已启动 (127.0.0.1:$EMBED_PORT, pid=$(cat "$PID_DIR/embed.pid"))"
      return 0
    fi
  done
  echo "  ❌ embed 启动失败 (见 $EMBED_LOG)"
  return 1
}


start_llm_proxy() {
  if _port_alive "$LLM_PROXY_PORT"; then
    echo "  ✅ llm-proxy 已在运行 (127.0.0.1:$LLM_PROXY_PORT)"
    return 0
  fi
  nohup python3 "$HERE/llm-proxy.py" --port "$LLM_PROXY_PORT" \
    >>"$HERE/llm-proxy.log" 2>&1 &
  echo $! > "$PID_DIR/llm-proxy.pid"
  for _ in $(seq 1 15); do
    sleep 1
    if _port_alive "$LLM_PROXY_PORT"; then
      echo "  ✅ llm-proxy 已启动 (127.0.0.1:$LLM_PROXY_PORT, pid=$(cat "$PID_DIR/llm-proxy.pid"))"
      return 0
    fi
  done
  echo "  ❌ llm-proxy 启动失败 (见 $HERE/llm-proxy.log)"
  return 1
}


start_gateway() {
  if _port_alive "$GW_PORT"; then
    echo "  ✅ Gateway 已在运行 (127.0.0.1:$GW_PORT)"
    return 0
  fi
  nohup bash "$HERE/start-gateway.sh" >>"$GW_LOG" 2>&1 &
  echo $! > "$PID_DIR/gateway.pid"
  for _ in $(seq 1 40); do
    sleep 1
    if _port_alive "$GW_PORT"; then
      echo "  ✅ Gateway 已启动 (127.0.0.1:$GW_PORT, pid=$(cat "$PID_DIR/gateway.pid"))"
      return 0
    fi
  done
  echo "  ❌ Gateway 启动失败 (见 $GW_LOG)"
  return 1
}


stop_one() {
  local name="$1" port="$2"
  if ! _port_alive "$port"; then
    echo "  · $name 未运行"
    rm -f "$PID_DIR/$name.pid"
    return 0
  fi
  # ⚠️ 用明确 PID, 不用 pkill -f (会自匹配并杀掉本 shell)
  local pid
  pid=$(_pid_of "$name")
  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    # pid 文件失效 → 扫 /proc 找监听该端口的进程
    pid=$(ss -lptn "sport = :$port" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1)
  fi
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null
    sleep 2
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null
    echo "  ✅ $name 已停止 (pid=$pid)"
  else
    echo "  · $name 未找到进程"
  fi
  rm -f "$PID_DIR/$name.pid"
}


case "${1:-status}" in
  start)
    echo "=== 启动 memory-tencentdb 组件 ==="
    start_embed || exit 1
    start_llm_proxy || exit 1
    start_gateway || exit 1
    echo "=== 完成 ==="
    ;;
  stop)
    echo "=== 停止 ==="
    stop_one gateway "$GW_PORT"
    stop_one llm-proxy "$LLM_PROXY_PORT"
    stop_one embed "$EMBED_PORT"
    ;;
  restart)
    bash "$HERE/supervise.sh" stop; sleep 2; bash "$HERE/supervise.sh" start
    ;;
  status)
    echo "=== memory-tencentdb 组件状态 ==="
    for pair in "gateway:$GW_PORT" "llm-proxy:$LLM_PROXY_PORT" "embed:$EMBED_PORT"; do
      name="${pair%%:*}"; port="${pair##*:}"
      if _port_alive "$port"; then
        echo "  ✅ $name  127.0.0.1:$port  (pid=$(_pid_of "$name"))"
      else
        echo "  ❌ $name  127.0.0.1:$port  未运行"
      fi
    done
    echo "  数据目录: ~/.memory-tencentdb/memory-tdai"
    echo "  配置:     $HERE/tdai-gateway.yaml"
    ;;
  *)
    echo "用法: $0 {start|stop|restart|status}" >&2
    exit 2
    ;;
esac
