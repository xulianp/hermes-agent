#!/usr/bin/env bash
# ============================================================
# memory-tencentdb Gateway 启动脚本
#
# 由 memory_tencentdb provider 的 GatewaySupervisor 自动调用
#   (MEMORY_TENCENTDB_GATEWAY_CMD 指向本脚本)
# 也可以手动跑:  bash ~/.hermes/memory-tencentdb/start-gateway.sh
#
# 职责: 注入 LLM 凭证与路径 → 启动 Node Gateway → 后台常驻
# ============================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
CORE="${TDAI_CORE_DIR:-$HOME/program/TencentDB-Agent-Memory/MemoryCore}"

# ── 注入 LLM 凭证 (从 Hermes 的 .env 读, 不硬编码) ──────────
if [[ -f "$HERMES_HOME/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$HERMES_HOME/.env"
  set +a
fi

# ⚠️ .env 里叫 MINIMAX_CN_API_KEY, Gateway 要的是 TDAI_LLM_API_KEY → 必须显式映射.
#    (Hermes 自己的 provider 读 MINIMAX_CN_API_KEY; Gateway 是独立进程, 不认识它.)
if [[ -z "${TDAI_LLM_API_KEY:-}" ]]; then
  TDAI_LLM_API_KEY="${MINIMAX_CN_API_KEY:-${MINIMAX_API_KEY:-}}"
fi

if [[ -z "${TDAI_LLM_API_KEY:-}" ]]; then
  echo "[gateway] FATAL: 未找到 LLM 凭证 (需要 TDAI_LLM_API_KEY, 或 ~/.hermes/.env 里的 MINIMAX_CN_API_KEY)" >&2
  exit 1
fi
export TDAI_LLM_API_KEY

# ⚠️ 走本地 llm-proxy (127.0.0.1:8422): MiniMax-M3 的 <think> 思考块会污染
#    JSON 输出 → 上游 l1-extractor PARSE_FAIL → L1 抽取恒为 0 → L2/L3 停滞。
#    代理只对"无 tools 且 prompt 含 L1 标记"的任务注入 thinking.type=disabled;
#    L2/L3 带 tools, 保留 thinking 不受影响。
#    (环境变量优先级高于 yaml, 所以这里必须改, 改 yaml 不生效。)
export TDAI_LLM_BASE_URL="${TDAI_LLM_BASE_URL:-http://127.0.0.1:8422/v1}"
export TDAI_LLM_MODEL="${TDAI_LLM_MODEL:-MiniMax-M3}"
export TDAI_GATEWAY_CONFIG="$HERE/tdai-gateway.yaml"
export TDAI_DATA_DIR="${TDAI_DATA_DIR:-$HOME/.memory-tencentdb/memory-tdai}"
export TDAI_GATEWAY_PORT="${TDAI_GATEWAY_PORT:-8420}"
export TDAI_GATEWAY_HOST="${TDAI_GATEWAY_HOST:-127.0.0.1}"

mkdir -p "$TDAI_DATA_DIR"

# ── 定位 node ───────────────────────────────────────────────
# 手动在 shell 里跑时有 Hermes 的 PATH；systemd 用户单元没有 →
# 回退到 Hermes 自带的 node (tools/node-*/bin/node)。
if ! command -v node >/dev/null 2>&1; then
  for d in "$HOME"/.hermes/tools/node-*/bin; do
    if [[ -x "$d/node" ]]; then
      PATH="$d:$PATH"
      break
    fi
  done
fi
if ! command -v node >/dev/null 2>&1; then
  echo "[gateway] FATAL: 找不到 node (PATH=$PATH)" >&2
  exit 1
fi
export PATH

# ── 启动 ────────────────────────────────────────────────────
cd "$CORE"
exec node --import tsx src/gateway/server.ts
