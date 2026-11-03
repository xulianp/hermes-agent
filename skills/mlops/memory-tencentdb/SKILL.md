---
name: memory-tencentdb
description: Use when operating or diagnosing memory-tencentdb.
version: 1.0.0
author: xlp
---

# memory-tencentdb 运维与验证

Hermes 的第二记忆模块 —— 与 builtin MEMORY/USER 并存的按需召回通道(四层: L0 对话 → L1 情节 → L2 场景块 → L3 persona)。部署架构/命令/配置决策以 `~/.hermes/memory-tencentdb/README.md` 为准,这里只放验证流程和坑。

## When to Use

- 用户问"memory 系统 / 第二记忆模块"的状态、报错、抽查、对账、核对测试结果。
- 起停/重启组件、查 embedding 是否真在工作、找孤儿记录、验证去重。
- 给这台机器选/接本地 embedding 模型。

## 组件位置(速查)

- 运维目录 `~/.hermes/memory-tencentdb/`: supervise.sh、tdai-gateway.yaml、embedding-shim.py、start-gateway.sh、{shim,gateway}.log
- 数据目录 `~/.memory-tencentdb/memory-tdai/`: records/*.jsonl(L1)、conversations/*.jsonl(L0)、vectors.db(检索库)、profiles/.../scene_blocks + persona.md(L2/L3)
- 上游源码 `/home/xlp/program/TencentDB-Agent-Memory/MemoryCore/`(Node/TS,入口 src/gateway/server.ts)
- Hermes 插件 symlink `~/.hermes/hermes-agent/plugins/memory/memory_tencentdb`

## 运维命令

```bash
bash ~/.hermes/memory-tencentdb/supervise.sh {start|stop|restart|status}
```

## 验证流程(结论先行,别信 status 全绿)

1. 健康探测会假阳性:`supervise.sh status` 与两个 `/health` 只证明进程在、端口通,不证明 embedding 上游可用。确认 embedding 真在工作:
   `grep -iE "insufficient balance|Embedding API error|502|upstream no vectors" gateway.log shim.log` —— 余额/额度耗尽只出现在日志里,`/health` 照样返回 ok。
2. 数据一致性对账(找孤儿记录): records/*.jsonl 行数 与 vectors.db 的 `l1_records`、`l1_fts` 行数三者必须一致;差额就是"写了 jsonl 但没进检索库"的孤儿(永远召不回)。跑 `scripts/reconcile_records.py`。
3. 向量是否真的在存:看 `l1_vec_rowids` 行数(才是向量条数);`l1_vec_chunks` 是 chunk 元数据不是向量条数 —— 10 条向量可能只占 1 个 chunk,拿它判断"空转"会误报。

## 坑(实测得出,都是机制不是偶发)

- **去重是"批内去重",不是"库内去重"** —— 同一事实跨会话/跨轮次各存一份。别把单批"3 条同义→1 条"当成全局去重已生效;要按 id 对账查重复。
- **健康端点假阳性** —— shim `/health` 无条件返回 ok(不查上游),gateway `/health` 的 embeddingService 只测 shim 可达。判定 embedding 好坏必须看日志,不能看 status。
- **jsonl 与向量库写入非原子** —— gateway 重启会丢掉"已 append jsonl 但没索引进向量库"的在途记录,启动后无 replay 恢复。每次重启后跑对账脚本。
- **vec0 虚拟表裸 sqlite3 读不了** —— `l1_vec`/`l0_vec` 是 vec0 扩展表,裸 python sqlite3 报 "no such module: vec0"。读 `l1_records`/`l1_fts`/`l1_vec_chunks` 这些普通表即可。
- **换 embedding 模型 = 换维度 = 重建向量表** —— `embedding_meta` 表钉死 dimensions,换不同维模型要重建;向量表接近空时重建代价≈0。⚠️ 实测(2026-11-03 换 bge-small-zh): 重建后**旧 L1 记录不会被自动重新嵌入** —— 上游 `reindexAll()` 定义了但全仓无人调用,`needsReindex` 标志是死代码;结果旧记忆只剩 BM25 召回,新记忆才有向量。要补向量得手动 re-embed 或重新 ingest。
- **checkpoint.json 会滞后/不一致** —— l0_conversations_count、last_persona_at 等与磁盘实际对不上,以 jsonl/db 为准,别拿它当唯一真相。

## 本地 embedding

见 `references/local-embedding.md`(模型选型、服务方式、下载源、维度迁移)。
