#!/usr/bin/env python3
"""
LLM 代理 —— 修复 MiniMax-M3 的 <think> 污染 JSON 问题

问题: MiniMax-M3 默认把 <think>...</think> 思考块混在 content 里。
上游 l1-extractor 直接 JSON.parse(content) → PARSE_FAIL → 抽取结果 0
→ L1 无新增 → L2 cursor 不推进 → L2/L3 永远滞后。

上游完全没有处理 <think> 的代码, 且 AI SDK 的 providerOptions 不透传
未知 body 字段 (实测 providerOptions.openai.thinking 被忽略)。

本代理在 LLM 请求体里注入 thinking.type=disabled (仅对需要纯 JSON 的任务),
MiniMax 收到后直接返回纯 JSON, 无 think 块。

只代理 OpenAI 兼容的 /v1/chat/completions; 其余路径 404 透传给上游。
不改上游仓库 (上游以 symlink 装入, 必须保持 git 干净)。

用法:
  python3 llm-proxy.py --port 8422 --upstream https://api.minimaxi.com/v1
"""
import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 这些 taskId 要求纯 JSON 输出 → 注入 thinking.type=disabled
JSON_TASKS = {"l1-extraction", "l1-dedup"}
# 调试开关: 设为 1 则所有任务都注入
FORCE_DISABLE_THINKING = os.environ.get("LLM_PROXY_FORCE_DISABLE_THINKING") == "1"

STATS = {"total": 0, "json_tasks": 0, "disabled": 0, "errors": 0,
         "status_enum_injected": 0, "transition_rule_injected": 0}
STATS_LOCK = threading.Lock()

# ---------------------------------------------------------------- P1-2
# work_task 的 metadata.status 受控词表 (上游 prompt 只说"可补充 status",
# 自由文本 → 实测库内出现 8 种发散取值: deployed/confirmed/paused/done/
# completed/fixed/deferred/residual_duplicates, 无流转语义)。
#
# 词表沿用上游 offload 模块已存在的取值 (src/offload/types.ts:70 与
# src/offload_server/task-transition.ts:105): done|doing|todo|paused|blocked,
# 补 cancelled 以覆盖"不再做"这一真实语义。
WORK_TASK_STATUS_VOCAB = "todo | doing | paused | blocked | done | cancelled"

# 注入到 L1 extraction 的 system prompt: 约束 status 取值 + 要求状态变更
# 时产出新记忆 (带新 status), 而不是把旧状态当永久事实。
STATUS_ENUM_RULE = """
---
### 任务状态字段规范（work_task / work_fact 的 metadata.status）

`metadata.status` 必须且只能取以下受控词表之一，不得自创同义写法：

- `todo` —— 已识别、尚未开始
- `doing` —— 正在进行
- `paused` —— 主动搁置、暂缓
- `blocked` —— 被外部依赖或决策阻塞
- `done` —— 已完成并已交付
- `cancelled` —— 明确不再做

规则：
1. 若任务当前状态不明，**不要**写 status 字段（输出 `{}` 或省略该键），不要用 `pending`/`new`/`confirmed`/`fixed`/`deployed` 这类非词表取值。
2. `status` 描述的是**该条记忆所记录时刻**的状态，不是任务的最终状态。
3. 若本轮消息表明某个**此前已存在**的任务状态发生了变化（例如从 todo 变为 done），必须为此产出一条新的 work_task 记忆，content 写明最新状态与依据，并让 metadata.status 取新值 —— 这样下游的 dedup 才能用 update 覆盖旧记录。不要只在 content 里含糊带过。
4. `content` 里不要写"尚未执行""待决策""计划中"这类会永久固化的时间性措辞，除非该任务的当前 status 确实是 todo/doing/paused/blocked。任务已 done/cancelled 时，content 应写完成或取消的事实。
"""

# 注入到 L1 dedup 的 system prompt: 状态变更走 update (覆盖旧记录),
# 而不是 merge (把新旧状态揉成一条, 导致状态永远停在旧值)。
TRANSITION_RULE = """
---
### 状态流转的处理规则（优先于上文的一般策略）

当候选记忆与新记忆描述**同一个任务**，且二者的 `metadata.status` 不同：
- 必须使用 `action: "update"`，`target_ids` 指向**全部**状态陈旧的旧记录；
- `merged_content` 写最新状态的事实（含完成/取消的时间与依据），不要保留旧状态措辞；
- `merged_type` 保持 `work_task`；
- `metadata` 不在输出格式内，所以最新 status 通过 `merged_content` 正文体现。

不要用 `merge` 处理状态变化 —— merge 会把"尚未完成"和"已完成"揉进同一条，
使该任务在后续召回中永远显示为未完成。
"""


def _messages_text(body: dict) -> str:
    """把请求里所有文本消息拼起来 (用于任务识别)。"""
    parts = []
    for m in body.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
    return "\n".join(parts)


def _append_to_system_prompt(body: dict, addition: str) -> bool:
    """往 system 消息末尾追加一段规范。

    AI SDK 的 system prompt 既可能是 messages[0].content 字符串,
    也可能是 role=system 的 content 数组。两种都处理。
    """
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return False
    # 优先找 role=system 的消息
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") != "system":
            continue
        c = m.get("content")
        if isinstance(c, str):
            m["content"] = c + addition
            return True
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    part["text"] = part["text"] + addition
                    return True
    # 没有 system 消息 → 造一个
    msgs.insert(0, {"role": "system", "content": addition})
    return True


def read_api_key() -> str:
    env_path = os.path.expanduser("~/.hermes/.env")
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("MINIMAX_CN_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("MINIMAX_CN_API_KEY not found in ~/.hermes/.env")


API_KEY = read_api_key()


def detect_json_task(body: dict) -> bool:
    """判定这次调用是否要求纯 JSON 输出 (需要关掉 MiniMax 的 <think>)。

    实测 (2026-11-04): AI SDK v6 / @ai-sdk/openai v3 **不会**把 taskId 或
    providerOptions.openai 里的自定义字段放进 HTTP body —— 发出的 body 只有
    {model, messages, max_tokens, user}。所以不能用 taskId 判别。

    改用两个可靠特征:
      1. body 里没有 tools → 纯文本任务 (L1 抽取 / L1 dedup);
         L2/L3 带 tools (读写 scene_blocks / persona.md), 保留 thinking。
      2. prompt 里含 L1 专属标记:
         - l1-extraction: "scene_name"
         - l1-dedup:      "merged_content" 且含 "record_id"
    两者同时满足才判为 JSON 任务 —— 避免误伤 L2/L3。
    """
    if body.get("tools"):
        return False  # 带工具的任务 = L2/L3, 需要推理, 不能禁 thinking

    text_parts = []
    for m in body.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str):
            text_parts.append(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
    text = "\n".join(text_parts)

    if "scene_name" in text:
        return True                      # l1-extraction
    if "merged_content" in text and "record_id" in text:
        return True                      # l1-dedup
    return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默默认 access log
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send(404, b'{"error":"llm-proxy: only /chat/completions is proxied"}')
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send(400, b'{"error":"llm-proxy: invalid JSON body"}')
            return

        is_json_task = detect_json_task(body)
        if is_json_task or FORCE_DISABLE_THINKING:
            # 注入 thinking 关闭 (MiniMax 专有字段; 走兼容模式直传 body)
            body.setdefault("thinking", {})["type"] = "disabled"
            # 有些部署用 reasoning_split 分离思考内容; 两者都设更稳
            body["reasoning_split"] = True
            with STATS_LOCK:
                STATS["json_tasks"] += 1
                STATS["disabled"] += 1

        # ---- P1-2: work_task 状态受控词表 + 状态流转规则 ----
        # 只对 L1 extraction / L1 dedup 注入 (与 JSON_TASKS 同一判据域),
        # L2/L3 带 tools, 不受影响。
        text = _messages_text(body)
        if not body.get("tools"):
            if "scene_name" in text and "merged_content" not in text:
                # l1-extraction → 注入 status 词表
                if _append_to_system_prompt(body, STATUS_ENUM_RULE):
                    with STATS_LOCK:
                        STATS["status_enum_injected"] += 1
            elif "merged_content" in text and "record_id" in text:
                # l1-dedup → 注入状态流转规则
                if _append_to_system_prompt(body, TRANSITION_RULE):
                    with STATS_LOCK:
                        STATS["transition_rule_injected"] += 1

        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.server.upstream + "/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {API_KEY}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = resp.read()
                with STATS_LOCK:
                    STATS["total"] += 1
                self._send(resp.status, data)
        except urllib.error.HTTPError as e:
            data = e.read()
            with STATS_LOCK:
                STATS["errors"] += 1
                STATS["total"] += 1
            self._send(e.code, data)
        except Exception as e:
            with STATS_LOCK:
                STATS["errors"] += 1
            self._send(502, json.dumps({"error": f"llm-proxy upstream: {e}"}).encode())

    def do_GET(self):
        if self.path.rstrip("/").endswith("/health"):
            with STATS_LOCK:
                s = dict(STATS)
            self._send(200, json.dumps({"status": "ok", "stats": s}).encode())
            return
        self._send(404, b'{"error":"llm-proxy: not found"}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8422)
    ap.add_argument("--upstream", default="https://api.minimaxi.com/v1")
    a = ap.parse_args()

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    srv.upstream = a.upstream.rstrip("/")
    print(f"llm-proxy listening on 127.0.0.1:{a.port} → {srv.upstream}", flush=True)
    print(f"  JSON-only tasks (thinking disabled): {sorted(JSON_TASKS)}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
