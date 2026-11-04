# memory-tencentdb 运维层

本目录是 **memory-tencentdb 第二记忆模块的本机运维层**，与上游
`NousResearch/hermes-agent` 代码无关，也不修改
`TencentDB-Agent-Memory/MemoryCore` 上游仓库（上游保持 git clean）。

运行时实际位置：`~/.hermes/memory-tencentdb/`。本目录是它的**版本化副本**，
便于审阅与复现；改完记得同步回 `~/.hermes/memory-tencentdb/`。

## 文件

| 文件 | 作用 |
|---|---|
| `supervise.sh` | 统一管理三个进程：`{start\|stop\|restart\|status}` |
| `start-gateway.sh` | Gateway 启动脚本，负责环境变量注入与凭据映射 |
| `tdai-gateway.yaml` | Gateway 配置（LLM / embedding / SQLite+向量库 / pipeline 阈值） |
| `llm-proxy.py` | MiniMax-M3 代理：注入 `thinking.type=disabled` 修 L1 JSON 解析；并注入 work_task 受控 status 词表与状态流转规则 |
| `local-embed.py` | 本地 embedding 服务（BAAI/bge-small-zh-v1.5, 512 维） |
| `embedding-shim.py` | 早期 MiniMax embedding 协议兼容脚本（已停用，保留备查） |
| `measure_recall.py` | 召回区分度量化：hit@5 + 组间 Jaccard + 随机基线（**含真实租户 ID 与私人记忆内容，已 gitignore，只在本机运行**） |
| `OPERATIONS.md` | 验证流程、坑与清理 SOP（原 `skills/mlops/memory-tencentdb`，skill 已删除） |
| `references/` | 污染审计、embedding 选型 |
| `scripts/` | 四层对账/污染扫描、L1 对账、补向量 |

Hermes 侧集成在 `plugins/memory_tencentdb/`（通用插件，`pre/post_llm_call` 钩子），
不占用 `memory.provider`，也不修改上游仓库。

## 端口

| 端口 | 组件 |
|---|---|
| 8420 | Gateway（`/capture`、`/v3/*`） |
| 8421 | 本地 embedding 服务 |
| 8422 | llm-proxy（LLM 请求代理） |

## 凭据

**不硬编码任何真值。** yaml 与脚本一律用 `${TDAI_LLM_API_KEY}` 之类的环境变量
引用，运行时从 `~/.hermes/.env` 读取。

⚠️ `.env` 里叫 `MINIMAX_CN_API_KEY`，Gateway 要的是 `TDAI_LLM_API_KEY`
——`start-gateway.sh` 负责显式映射。改配置时注意
**环境变量优先级高于 yaml**：`TDAI_LLM_BASE_URL` 会覆盖
`tdai-gateway.yaml` 的 `llm.baseUrl`，改 yaml 不生效时先查 `start-gateway.sh`。

## 关键约束（改代码前必读）

1. **写 L1 用 `/v3/conversation/add`，不要用 `/capture`。**
   `/capture` 的 `handleCapture` 只把 `sessionKey`/`sessionId` 传给
   `handleTurnCommitted`，**丢弃 user_id**；而召回侧
   `rowMatchesIsolation` 对 `user_id` 做**精确相等**比较 →
   用 `/capture` 写入的记忆 Hermes 永远召不回。
   `/v3/conversation/add` 保留 `iso.userId` 且会调 `notifyPipeline` 触发 L1/L2/L3。

2. **llm-proxy 只对 L1 抽取/去重注入，判据是"无 tools + prompt 含 L1 标记"。**
   L2/L3 带 tools（读写 scene_blocks / persona.md），保留 thinking 以免推理质量下降。

3. **改完任何 L1 数据都要对账四路**：jsonl / `l1_records` / `l1_fts` / `l1_vec_rowids`。
   `atomic/delete` 与 `atomic/update` **不写 `records/*.jsonl`**，需手工同步；
   L1 dedup 的 merge 会删旧 ID 写新 ID，旧 ID 在 jsonl 里成孤儿。

4. **`atomic/update` 有上游 bug**：`queryL1Records({recordIds})` 静默忽略
   `recordIds`，返回全表第一行 → 把别的记录的 metadata/type 写进目标记录。
   改 content 用 `atomic/update`（会重嵌入），**metadata 一律手工改 DB**。

详见 `ops/memory-tencentdb/OPERATIONS.md` 的"坑"一节。
