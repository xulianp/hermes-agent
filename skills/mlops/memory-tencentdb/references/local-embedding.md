# 本地 embedding 选型与接入(CPU-only / 8GB / 中文记忆)

## 结论

这台机器(Ryzen 3 2200G、无 CUDA、~3.4Gi 可用内存、记忆全中文)用 **BAAI/bge-small-zh-v1.5**(24M 参数、512 维、~95MB、峰值 ~500MB)。要更好质量上 bge-base-zh-v1.5(768 维/~400MB),内存仍够。别选 bge-m3(2.2GB)、nomic-embed-text(中文弱)。

## 下载源(网络实测)

- huggingface.co 直连超时;hf-mirror.com 与 modelscope.cn 可用。模型下载走 `HF_ENDPOINT=https://hf-mirror.com` 或 modelscope。
- pip 装依赖:pypi.org 直连通,清华镜像也通。

## 服务方式

- 用 uv 建独立 venv(机器有 uv;hermes python 3.14 无 pip、系统 python 3.8 太旧):`uv venv --python 3.12` + `uv pip install sentence-transformers`。
- 写一个 OpenAI 兼容 `/v1/embeddings` 本地服务(复用 embedding-shim.py 的 http.server 骨架),监听本地端口。
- **绕过 shim**:embedding-shim.py 是 OpenAI→MiniMax 协议翻译;本地模型是 OpenAI→OpenAI,而 Gateway 原生讲 OpenAI 协议 → 直接改 tdai-gateway.yaml 的 `embedding.baseUrl` 指向本地服务,shim 变多余。
- 改 yaml:`embedding.model` → bge 模型名、`dimensions` 1536→512、`sendDimensions` 保持 false。

## 选型对比(Jina v5-text,2026-02 发布)

- jina-embeddings-v5-text-small: 677M / 1024 维 / 119 语种 / MMTEB 67.0,质量 SOTA,但 ~1.3GB 权重,3.4Gi 内存 CPU-only 跑不动(int8 勉强,质量掉)。
- jina-embeddings-v5-text-nano: 239M / 768 维 / 15+ 语种,底座 EuroBERT,中文弱。
- 结论: 中文单语 + 8GB 内存,选 bge-small-zh-v1.5;等升内存/混多语种再考虑 jina-v5-small。

## 已落地(2026-11-03)

本机已切到本地 bge-small-zh-v1.5,文件与命令如下(实测跑通):

- 服务脚本 `~/.hermes/memory-tencentdb/local-embed.py`(OpenAI 兼容 /v1/embeddings,懒加载,normalize_embeddings=True)。
- `supervise.sh` 已从管理 shim 改为管理 `embed`(local-embed.py,端口 8421 不变);旧 embedding-shim.py 保留作回退。
- venv: `~/.hermes/memory-tencentdb/.venv`(python 3.11,uv 建)。
- 模型: `~/.hermes/memory-tencentdb/models/bge-small-zh-v1.5`(95MB,512 维)。
- yaml: `embedding.model=BAAI/bge-small-zh-v1.5`、`dimensions=512`、`timeoutMs=60000`;baseUrl 仍是 127.0.0.1:8421/v1。

安装命令(实测):
```bash
UV=/home/xlp/.hermes/tools/uv-0.12.3-linux-x64/uv
cd ~/.hermes/memory-tencentdb
$UV venv --python 3.11 .venv          # 3.11 已在 uv 本地缓存,免下载
$UV pip install --python .venv/bin/python torch --index-url https://download.pytorch.org/whl/cpu   # 必须 CPU 索引,否则拉 CUDA 版 ~2.5GB
$UV pip install --python .venv/bin/python sentence-transformers --index-url https://pypi.tuna.tsinghua.edu.cn/simple  # pypi 直连太慢,清华镜像快
HF_ENDPOINT=https://hf-mirror.com .venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('BAAI/bge-small-zh-v1.5', local_dir='models/bge-small-zh-v1.5')"
```

坑(实测):
- **uv pip install torch 必须用 pytorch CPU 索引**,默认 PyPI 的 torch 是 CUDA 版(几 GB);`--index-url` 只影响 torch 本身,deps 仍从默认源。
- **pypi 直连慢**(torch deps 下载 20+ 分钟仍卡在 tokenizers),换清华镜像秒装。
- 改完 yaml 重启 gateway 会自动检测维度变化(1536→512)、drop vec0 表重建、embedding_meta 更新为 512 —— 这段是自动的,不用手删 vectors.db。
- **`supervise.sh restart` 里 `"$0"` 是相对路径会报"未找到命令"** —— 已改成 `bash "$HERE/supervise.sh"`。
- 别用 `pkill -f "uv pip install"` 之类自匹配命令(会杀掉自己的 shell)。
