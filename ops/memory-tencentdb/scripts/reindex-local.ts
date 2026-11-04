// 一次性脚本: 换 embedding 模型(维度变了)后, 用本地 embedding 服务重新嵌入已有的 L1/L0 记录。
// 为什么需要手动跑: 上游 reindexAll() 定义了但全仓无人调用, needsReindex 标志是死代码,
//   换维度后 gateway 只会 drop 重建空的 vec0 表, 不会重新嵌入旧记录 → 旧记忆只剩 BM25。
// 用法(必须在 MemoryCore 目录下):  node --import tsx reindex-local.ts
// 跑完验证: l1_vec_rowids 行数 == l1_records 行数; 再搜一次看 strategy 变 hybrid、Vector search 返回 N>0。
import { VectorStore } from "./src/core/store/sqlite.js";

const DB = `${process.env.HOME}/.memory-tencentdb/memory-tdai/vectors.db`;  // 数据目录下的 vectors.db
const EMBED_URL = "http://127.0.0.1:8421/v1/embeddings";          // 本地 embedding 服务 (local-embed.py)
const MODEL = "BAAI/bge-small-zh-v1.5";                            // 与 tdai-gateway.yaml 一致
const DIMS = 512;                                                  // 与 tdai-gateway.yaml 一致

async function embedFn(text: string): Promise<Float32Array> {
  const res = await fetch(EMBED_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ input: [text], model: MODEL }),
  });
  const j: any = await res.json();
  const emb = j?.data?.[0]?.embedding;
  if (!emb) throw new Error("embed failed: " + JSON.stringify(j).slice(0, 200));
  return new Float32Array(emb);
}

async function main() {
  const store = new VectorStore(DB, DIMS);
  const initRes = store.init({ provider: "openai", model: MODEL });
  console.log("init:", JSON.stringify(initRes));
  const r = await store.reindexAll(embedFn, (done, total, layer) => {
    console.log(`${layer} ${done}/${total}`);
  });
  console.log("reindex result:", JSON.stringify(r));
}

main().then(() => process.exit(0)).catch((e) => { console.error(e); process.exit(1); });
