# memory-tencentdb 运维与验证

> 原 `skills/mlops/memory-tencentdb` 的运维知识；skill 已删除，内容迁到这里。
> 插件代码：`plugins/memory_tencentdb/`。起停命令：`hermes memory-tencentdb {start|stop|restart|status}`。

Hermes 的记忆后端 —— 与 builtin MEMORY/USER 并存的按需召回通道(四层: L0 对话 → L1 情节 → L2 场景块 → L3 persona)。部署架构/命令/配置决策以 `~/.hermes/memory-tencentdb/README.md` 为准,这里只放验证流程和坑。

## 适用场景

- 排查"memory 系统 / 四层记忆"的状态、报错、抽查、对账、核对测试结果。
- 起停/重启组件、查 embedding 是否真在工作、找孤儿记录、验证去重。
- 给这台机器选/接本地 embedding 模型。

## 组件位置(速查)

- 运维目录 `~/.hermes/memory-tencentdb/`: supervise.sh、tdai-gateway.yaml、start-gateway.sh、local-embed.py(当前 embedding 服务)、embedding-shim.py(旧,已停用)、{embed,gateway}.log
- 数据目录 `~/.memory-tencentdb/memory-tdai/`: records/*.jsonl(L1)、conversations/*.jsonl(L0)、vectors.db(检索库)、profiles/.../scene_blocks + persona.md(L2/L3)
- 上游源码 `~/program/TencentDB-Agent-Memory/MemoryCore/`(Node/TS,入口 src/gateway/server.ts)
- Hermes 通用插件 `plugins/memory_tencentdb/`（`pre/post_llm_call` 钩子；不再走 `memory.provider` 或 `plugins/memory/` symlink）

## 运维命令

```bash
bash ~/.hermes/memory-tencentdb/supervise.sh {start|stop|restart|status}
```

**进程 cgroup 必须不是 `hermes-gateway.service`** —— gateway 停止时会 `gateway.cgroup_cleanup` 清空自己的
cgroup，sidecar 若在里面就一起被杀，重启后第一轮消息的 L0 写入直接 `Connection refused`。用
`ops/memory-tencentdb/memory-tencentdb.service`（`Type=oneshot` + `RemainAfterExit`）：

```bash
cp ~/.hermes/hermes-agent/ops/memory-tencentdb/memory-tencentdb.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now memory-tencentdb.service
```

判据：`grep '^1:name=systemd' /proc/<pid>/cgroup` 应显示 `…/memory-tencentdb.service`。
单元存在时插件和 `hermes memory-tencentdb` 都改走 `systemctl --user`，否则退回 `supervise.sh`。

## 验证流程(结论先行,别信 status 全绿)

1. 健康探测会假阳性:`supervise.sh status` 与两个 `/health` 只证明进程在、端口通,不证明 embedding 上游可用。确认 embedding 真在工作:
   `grep -iE "insufficient balance|Embedding API error|upstream no vectors|vector search failed" gateway.log` —— 余额/额度耗尽只出现在日志里,`/health` 照样返回 ok。
   ⚠️ **别把裸 `502` 放进这个 grep**:DEBUG 行里的本地端口号和毫秒时间戳会命中它(`src=127.0.0.1:55028` / `t=1793751502160`),几百条假阳性会淹掉真实错误。要按状态字段匹配(`status=502`)或只看带 `error|ERROR` 的行。
   ⚠️ **日志跨架构变更留历史**:被换掉的子进程(旧 shim/旧模型)的报错可能早于当前进程启动。判断"现在坏没坏"要按**进程启动时间切窗口**,别把整份日志的累计命中当现状(拿 `ls -l --time-style=full-iso` 比对日志 mtime 与进程 start time)。
2. 数据一致性对账(找孤儿记录): records/*.jsonl 行数 与 vectors.db 的 `l1_records`、`l1_fts`、`l1_vec_rowids` 行数必须一致;差额就是"写了 jsonl 但没进检索库"的孤儿(永远召不回)。跑 `scripts/reconcile_records.py`(仅 L1)或 `scripts/audit_layers.py`(L0-L3 四层 + 污染扫描)。
3. 向量是否真的在存:看 `l1_vec_rowids` 行数(才是向量条数);`l1_vec_chunks` 是 chunk 元数据不是向量条数 —— 10 条向量可能只占 1 个 chunk,拿它判断"空转"会误报。
4. 换 embedding 模型后,真正生效的判据是**向量召回端到端命中**:搜索日志里 `Vector search returned N candidates`(N>0)且 `strategy=hybrid`。"无 insufficient balance 报错"只证明 embedding 服务通了,不证明旧记录有向量 —— 向量库空时搜索会静默掉进 BM25 兜底(strategy=fts)还照样返回结果,容易误判成"向量已生效"。
5. **跨层污染审计**(任何写入测试之后必做):一次写入会沿 L1→L2→L3 扩散,而 L3 persona 是**每轮无条件注入**的 —— 污染的最终代价由顶层承担,只查 L1 会漏掉真正会伤到用户的那条。逐层独立查,再从召回侧验一次(用污染关键词直接搜,确认假事实能否被召回)。跑 `scripts/audit_layers.py --flags <关键词>`,详见 `references/contamination-audit.md`。
6. **清理期的数据目录可能已经变了** —— pipeline 与清理并发,场景块/persona 会被自动改写(见"坑"里的 pipeline 异步条目)。所以**删之前重读一次目标**,别用上一步的读结果;验 persona 之后再看一眼 `last_persona_time` 是否推进。单次"验过了"不等于清理完成。

## 坑(实测得出,都是机制不是偶发)

- **去重是"批内去重",不是"库内去重"** —— 同一事实跨会话/跨轮次各存一份。别把单批"3 条同义→1 条"当成全局去重已生效;要按 id 对账查重复。
- **健康端点假阳性** —— shim `/health` 无条件返回 ok(不查上游),gateway `/health` 的 embeddingService 只测 shim 可达。判定 embedding 好坏必须看日志,不能看 status。
- **jsonl 与向量库写入非原子** —— gateway 重启会丢掉"已 append jsonl 但没索引进向量库"的在途记录,启动后无 replay 恢复。每次重启后跑对账脚本。
- **vec0 虚拟表裸 sqlite3 读不了** —— `l1_vec`/`l0_vec` 是 vec0 扩展表,裸 python sqlite3 报 "no such module: vec0"。读 `l1_records`/`l1_fts`/`l1_vec_rowids`(向量条数)这些普通表即可。
- **换 embedding 模型 = 换维度 = 重建向量表** —— `embedding_meta` 表钉死 dimensions,换不同维模型要重建;向量表接近空时重建代价≈0。⚠️ 实测(2026-11-03 换 bge-small-zh): 重建后**旧 L1 记录不会被自动重新嵌入** —— 上游 `reindexAll()` 定义了但全仓无人调用,`needsReindex` 标志是死代码;结果旧记忆只剩 BM25 召回,新记忆才有向量。要补向量跑 `scripts/reindex-local.ts`(复制到 MemoryCore 目录,`node --import tsx` 跑,重新嵌入所有 L1/L0 文本)。
- **checkpoint.json 会滞后/不一致** —— l0_conversations_count、last_persona_at 等与磁盘实际对不上,以 jsonl/db 为准,别拿它当唯一真相。
- **多层记忆的写入语料必须隔离** —— 向 L0/L1 写一条"事实",它会被抽取成 L1 记忆、聚合成 L2 场景块、蒸馏成 L3 persona 规则;persona 每轮注入,于是假规则变成常驻的行为约束。写测试语料要么用**可一次性识别并清除**的内容,要么**先定好下游清理范围**再写;绝不能用真实用户事实当测试语料(污染会以最高优先级留在顶层)。
- **关键词扫描会误判"提及污染的记录"** —— 复盘时按关键词标记污染,那条"某次测试用了 X 编造数据"的记录本身含该关键词,但它陈述的是**事件**而不是**事实**,不该删。判定依据是记录断言了什么,不是它包含哪些词。
- **顶层不会自动给污染打标** —— L2 场景块会被补上"演化记录/来源待溯源"的免责说明,L3 persona 里对应的假规则**不会**被标注,照常注入。别因为场景块已标注就判定顶层干净。
- **表主键列名不统一** —— `l1_records` / `l1_fts` 的主键列是 `record_id`,`l1_vec_rowids` 用的是 `id`;写对账 SQL 时别混用,否则报 `no such column` 而误判成表不存在。
- **MiniMax-M3 的 `<think>` 会静默杀死 L1 抽取(2026-11-04 实测)** —— MiniMax 默认把思考块混在 `content` 里(上游完全无处理,AI SDK 的 `providerOptions` 也不透传未知 body 字段)。上游 `l1-extractor` 直接 `JSON.parse(content)` → `PARSE_FAIL` → **抽取结果恒为 0** → L1 无新增 → L2 cursor 不推进 → L2/L3 永远滞后。判据: 日志里 `PARSE_FAIL rawFull="<think>We need..."` + `L1 complete: extracted=0`。修法: 本地 `llm-proxy.py`(127.0.0.1:8422) 对"无 tools 且 prompt 含 L1 标记"的任务注入 `thinking.type=disabled`; L2/L3 带 tools 故保留 thinking。⚠️ 判据**不能用 taskId** —— AI SDK v6 发出的 body 只有 `{model, messages, max_tokens, user}`。
- **环境变量优先级高于 yaml** —— `TDAI_LLM_BASE_URL` 会覆盖 `tdai-gateway.yaml` 的 `llm.baseUrl`。改 yaml 不生效时,先查 `start-gateway.sh` 和进程 environ(`/proc/<pid>/environ`)。
- **`atomic/update` 不写 `records/*.jsonl`** —— 与 `atomic/delete` 同款缺口: API 改完 DB(含 FTS 与向量重建)但 jsonl 原样保留 → 对账差额。改完必须手工同步 jsonl。
- **L1 dedup 的 `merge/update` 会删旧记录写新记录** —— 手工改过的 L1 会被下一轮抽取的 merge 吸收成新 ID(内容取自 LLM 重读的事实),旧 ID 只剩在 jsonl 里成孤儿。看到 jsonl > db 的差额时先查这个,不是原子写缺口。
- **`work_task` 的 `metadata.status` 无状态流转** —— 抽取时把"当时的状态"写成永久事实,完成后没有条目去覆盖它。已完成的待办会持续被召回并误导决策。修法: 手工更新 content + `metadata.status=done`,或等下一轮抽取让 dedup merge 覆盖。
- **`/v3/atomic/delete` 不删 jsonl** —— 实测(2026-11-04 污染清理):API 删掉 L1 后 `l1_records`/`l1_fts`/`l1_vec_rowids` 三路都少 1,但 `records/*.jsonl` **原样保留**该条 → jsonl 计数比 DB 多,看起来像"写了没索引"的孤儿,实际是"删了没同步"。删完必须手工改 jsonl(或跑对账脚本修),否则:污染源仍在磁盘文件里(重启可能重新入库),且对账脚本会一直报假孤儿。
- **`/v3/core/write` 会 strip 掉场景导航** —— 它写入前调 `stripSceneNavigation(content)`,所以**不能靠 core/write 写导航**;导航是派生段,由 `refreshPersonaNavigation`(profile-sync 时)或 `updateSceneNavigation`(L2 跑完时)重建。删了场景块后导航会残留旧条目,要手动重建:用上游 `generateSceneNavigation(index)` + `readSceneIndex(dataDir)` 生成后拼在 persona 正文后写盘(脚本放 MemoryCore/src/ 下才能用相对路径 import,`tsx` 直接跑;用完即删)。
- **user_id 隔离是精确相等,写错就整池召回不到(2026-11-04 P1-1 实测)** —— `rowMatchesIsolation` 用 `row.user_id !== filter.userId` **严格相等**排除,不是"没传就不过滤"。Hermes provider 写入走 `/v3/conversation/add`(保留 `iso.userId`,并调 `notifyPipeline` 触发 L1/L2/L3),召回走 `atomic_search(user_id=self._user_id)`;两边都是真实飞书 ID,链路自洽。但**手动调 `/capture` 时 `handleCapture` 只把 `sessionKey`/`sessionId` 传给 `handleTurnCommitted`,user_id/team_id/agent_id 被完全丢弃** → 落库 user_id 变成 `default` → Hermes 永远召不回。判据: `SELECT user_id,COUNT(*) FROM l1_records GROUP BY 1` 出现 `default` 与真实 ID 并存,就是混写了。修法: 数据层 UPDATE 统一(userId 字段在 jsonl 里是**驼峰 `userId`**,DB 里是 `user_id`);或改用 `/v3/conversation/add` 写入。⚠️ 写完必须同时改 DB(`l1_records`/`l1_fts`/`l0_conversations`)与 jsonl(`records/`+`conversations/`)两处,只改一处会分叉。
- **`/capture` 与 `/v3/conversation/add` 不可互换** —— 前者丢 user_id,后者保留且触发 pipeline;要"写入并推进 L1/L2/L3"就用后者(需带 `team_id`/`agent_id`/`user_id`)。
- **`atomic/update` 会把 metadata 和 type 串到别的记录上(上游 bug, 2026-11-04 实测)** —— `handleAtomicUpdate` 用 `store.queryL1Records({recordIds:[id]})` 取原记录, 但 `queryL1Records` **只处理 sessionKey/sessionId/taskId/updatedAfter, `recordIds` 被静默忽略**, 于是返回**全表第一行**。结果: 你改 A 记录的 content, 却把 B 记录的 metadata/type 写进了 A。实测污染了 1 条 work_task (被写成 work_artifact + 别人的 artifact_ref)。修法: 用 `atomic/update` 改 content(会重嵌入, 保留向量正确性), **metadata 一律手工改 DB**(`l1_records.metadata_json`), 改完核对 `type` 没被带偏; 或直接改 DB + 用 `scripts/reindex-local.ts` 补向量。⚠️ 改 metadata 不影响向量与 FTS, 所以 metadata 修正不需要重嵌入。
- **`work_task` 的 `metadata.status` 无受控词表(2026-11-04 P1-2 修复)** —— 上游 prompt 只说"可补充 status", 模型自由发挥 → 实测库内出现 8 种发散值(deployed/confirmed/completed/fixed/deferred/residual_duplicates/paused/done), 无流转语义。修法(落本机层, 上游不动): `llm-proxy.py` 向 L1 extraction 的 system prompt 注入受控词表 `todo|doing|paused|blocked|done|cancelled`, 向 L1 dedup 注入"状态变更走 update 覆盖旧记录、不要 merge"规则。判据用已有的"无 tools + prompt 含 scene_name / merged_content"同一套特征, 不新增判据。验收: 新建进行中任务 → status=`doing`; 任务完成 → 旧 todo 记录被替换为 done(而非两条并存)。历史不合规记录手工迁移并留 `status_normalized_from` 溯源。
- **`atomic/update` 改 content 会保留旧 metadata** —— 上游 `parseMetadataJson(record.metadata_json)` 原样带回, 不会清空。所以要"重写一条记录的类型/元数据"时, update 只负责 content, 其余手工改 DB。
- **`atomic/delete` 的参数是 `ids` 数组, 不是 `id`** —— body `{"ids":[...]}`, 传 `id` 会 400。
- **pipeline 会异步改动同一批产物** —— L2/L3 挂在墙钟定时器上,不跟对话同步。清理过程中它可能自己重写场景块、自己重建 persona,于是"你据以决策的内容"在你动手前就变了,你的验证结论也可能在 pipeline 再跑一轮后过期。清理前先记 `checkpoint.json` 的 `last_persona_time`/`scenes_processed` 作判据,每次读产物前重读一遍,验 persona 前先看 `last_persona_time` 是否比自己写入的时刻新。详见 `references/contamination-audit.md`。

## L2/L3 数据面端点 (改场景块与 persona 用)

`/v3/scenario/*` 与 `/v3/core/*` 是 L2/L3 的读写面, 都走 `POST`, body 带
`team_id`/`agent_id`/`user_id`(缺省会落到 default bucket)。认证走
`x-tdai-service-id` + `Authorization` header, 不是 body。

| 端点 | body | 行为 |
|---|---|---|
| `/v3/scenario/ls` | `path_prefix?` | 列出场景块 + summary + version |
| `/v3/scenario/read` | `path` | 读单个场景块全文 |
| `/v3/scenario/write` | `path`, `content`, `summary?` | 写/覆盖, **同步刷新 scene_index.json** |
| `/v3/scenario/rm` | `path` | 删除场景块, **同步移除索引条目** |
| `/v3/core/read` | — | 读 persona (含导航) |
| `/v3/core/write` | `content` | 写 persona, **会 strip 导航**(见坑) |

`path` 必须是不含 `..` 的相对路径(服务端有 `safePath` 校验)。`scenario/rm` 与
`scenario/write` 都会把 scene_index 同步好 —— 删完块不用手工改索引。

## 脚本

- `scripts/reconcile_records.py` —— 对账 jsonl vs 检索库,找孤儿 L1
- `scripts/audit_layers.py` —— 四层(L0/L1/L2/L3)对账 + 污染关键词扫描,只读
- `scripts/reindex-local.ts` —— 换 embedding 模型后给旧记录补向量

## 本地 embedding

见 `references/local-embedding.md`(模型选型、服务方式、下载源、维度迁移)。
