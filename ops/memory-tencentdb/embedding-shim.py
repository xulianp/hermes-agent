#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
embedding-shim.py —— OpenAI embeddings 协议 → MiniMax embeddings 协议 的本地转换层

═══════════════════════════════════════════════════════════════════════
为什么需要它
═══════════════════════════════════════════════════════════════════════
memory-tencentdb Gateway 的远程 embedding 客户端写死 OpenAI 协议:
    发出:  POST {baseUrl}/embeddings   {input: [...], model, dimensions?}
    期望:  {data: [{index, embedding: [...]}, ...]}

MiniMax 的 embedding 接口协议不同:
    发出:  POST https://api.minimaxi.com/v1/embeddings  {texts: [...], type: "db"|"query", model}
    返:    {vectors: [[...], ...], base_resp: {status_code, status_msg}}

→ 本 shim 监听 127.0.0.1:8421, 把 OpenAI 形状翻译成 MiniMax 形状再翻译回来.
   Gateway 侧只需把 embedding.baseUrl 指向 http://127.0.0.1:8421/v1.

⚠️ **type 参数的语义** (MiniMax 特有):
    "db"    = 文档侧向量 (写入库里的记忆用这个)
    "query" = 查询侧向量 (检索时用这个)
   两者在同一空间, 但用途不同 → 本 shim 按**请求路径/用途**无法区分, 故:
   默认全部用 "db"; 若 Gateway 后续需要 query 侧, 可按请求头区分.
   ⚠️ 实际影响: 本项目 dedup 用向量做**候选召回**, 同一侧即可 (见实测报告).
"""
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("MINIMAX_EMBED_BASE_URL", "https://api.minimaxi.com/v1")
MODEL = os.environ.get("MINIMAX_EMBED_MODEL", "embo-01")
PORT = int(os.environ.get("EMBED_SHIM_PORT", "8421"))
API_KEY = os.environ.get("MINIMAX_CN_API_KEY", "")
EMBED_TYPE = os.environ.get("MINIMAX_EMBED_TYPE", "db")
LOG = os.environ.get("EMBED_SHIM_LOG", "")


def _log(msg: str) -> None:
    if LOG:
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默默认 stderr 日志
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        if not self.path.rstrip("/").endswith("/embeddings"):
            self._send(404, {"error": {"message": "only /embeddings is supported"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            req = json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:
            self._send(400, {"error": {"message": f"bad request: {e}"}})
            return

        # OpenAI 输入 → MiniMax 输入
        texts = req.get("input")
        if isinstance(texts, str):
            texts = [texts]
        if not isinstance(texts, list) or not texts:
            self._send(400, {"error": {"message": "input must be a non-empty array"}})
            return
        texts = [str(t) for t in texts]

        upstream_body = {
            "model": req.get("model") or MODEL,
            "texts": texts,
            "type": EMBED_TYPE,
        }
        # ⚠️ MiniMax 不认 dimensions → 不透传 (sendDimensions=false 时本来就不传)

        url = f"{UPSTREAM.rstrip('/')}/embeddings"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        }
        _log(f"[embed-shim] POST {url} n={len(texts)} model={upstream_body['model']} type={EMBED_TYPE}")

        try:
            ureq = urllib.request.Request(
                url, data=json.dumps(upstream_body).encode("utf-8"),
                headers=headers, method="POST")
            with urllib.request.urlopen(ureq, timeout=60) as r:
                up = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:400]
            _log(f"[embed-shim] upstream HTTP {e.code}: {detail}")
            self._send(502, {"error": {"message": f"upstream HTTP {e.code}: {detail}"}})
            return
        except Exception as e:
            _log(f"[embed-shim] upstream error: {e}")
            self._send(502, {"error": {"message": f"upstream error: {e}"}})
            return

        # MiniMax 输出 → OpenAI 输出
        vectors = up.get("vectors")
        if not vectors:
            base = up.get("base_resp") or {}
            msg = base.get("status_msg") or "upstream returned no vectors"
            _log(f"[embed-shim] upstream no vectors: {msg}")
            self._send(502, {"error": {"message": msg}})
            return

        data = [{"object": "embedding", "index": i, "embedding": v}
                for i, v in enumerate(vectors)]
        _log(f"[embed-shim] ok dims={len(vectors[0]) if vectors else 0} n={len(data)}")
        self._send(200, {"object": "list", "data": data,
                         "model": upstream_body["model"],
                         "usage": {"prompt_tokens": 0, "total_tokens": 0}})

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") == "/health":
            self._send(200, {"status": "ok", "upstream": UPSTREAM, "model": MODEL,
                             "type": EMBED_TYPE})
            return
        self._send(404, {"error": {"message": "not found"}})


if __name__ == "__main__":
    if not API_KEY:
        raise SystemExit("FATAL: MINIMAX_CN_API_KEY 未设置")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[embed-shim] listening on 127.0.0.1:{PORT} → {UPSTREAM}/embeddings "
          f"(model={MODEL}, type={EMBED_TYPE})", flush=True)
    srv.serve_forever()
