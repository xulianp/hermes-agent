#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""local-embed.py —— 本地 bge-small-zh-v1.5 的 OpenAI 兼容 /v1/embeddings 服务

替代 embedding-shim.py(OpenAI→MiniMax 协议翻译)。Gateway 原生讲 OpenAI 协议,
本服务直接提供 OpenAI 兼容接口,无需翻译,也无需任何外部 API key。

环境变量:
  LOCAL_EMBED_MODEL   模型路径或 HuggingFace 模型名 (默认 BAAI/bge-small-zh-v1.5)
  EMBED_SHIM_PORT     监听端口 (默认 8421)
  EMBED_SHIM_LOG      日志文件路径 (空 = 不写日志)

协议(与 Gateway 的 remote embedding 客户端对齐):
  请求: POST /v1/embeddings  {input: str|[str], model?}
  响应: {object:"list", data:[{object:"embedding", index, embedding:[...]}], model}
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = os.environ.get("LOCAL_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
PORT = int(os.environ.get("EMBED_SHIM_PORT", "8421"))
LOG = os.environ.get("EMBED_SHIM_LOG", "")

# 模型懒加载: 首次请求才加载(避免启动即占内存, 端口秒起)
_model = None


def _log(msg: str) -> None:
    if LOG:
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass


def _load_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _log(f"[local-embed] loading model {MODEL} ...")
        _model = SentenceTransformer(MODEL)
        _log(f"[local-embed] loaded dim={_model.get_sentence_embedding_dimension()}")
    return _model


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

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") == "/health":
            self._send(200, {"status": "ok", "model": MODEL})
            return
        self._send(404, {"error": {"message": "not found"}})

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

        texts = req.get("input")
        if isinstance(texts, str):
            texts = [texts]
        if not isinstance(texts, list) or not texts:
            self._send(400, {"error": {"message": "input must be a non-empty array"}})
            return
        texts = [str(t) for t in texts]

        try:
            model = _load_model()
            vecs = model.encode(texts, normalize_embeddings=True).tolist()
        except Exception as e:
            _log(f"[local-embed] encode error: {e}")
            self._send(500, {"error": {"message": f"encode error: {e}"}})
            return

        data = [{"object": "embedding", "index": i, "embedding": v}
                for i, v in enumerate(vecs)]
        self._send(200, {"object": "list", "data": data, "model": MODEL,
                         "usage": {"prompt_tokens": 0, "total_tokens": 0}})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[local-embed] listening on 127.0.0.1:{PORT} model={MODEL}", flush=True)
    srv.serve_forever()
