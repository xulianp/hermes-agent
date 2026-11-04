#!/usr/bin/env python3
"""对账 memory-tencentdb: 找写了 records/*.jsonl 但没进 vectors.db 的孤儿 L1 记录(永远召不回)。

只读,不修改任何数据。用法: python3 reconcile_records.py
"""
import glob
import json
import os
import sqlite3
import sys

DATA = os.path.expanduser("~/.memory-tencentdb/memory-tdai")
DB = os.path.join(DATA, "vectors.db")


def latest(pattern):
    files = sorted(glob.glob(os.path.join(DATA, pattern)))
    return files[-1] if files else None


def main():
    rec_file = latest("records/*.jsonl")
    if not rec_file:
        sys.exit("未找到 records/*.jsonl")

    try:
        c = sqlite3.connect(DB)
        n_rec = c.execute("SELECT COUNT(*) FROM l1_records").fetchone()[0]
        n_fts = c.execute("SELECT COUNT(*) FROM l1_fts").fetchone()[0]
        n_vec = c.execute("SELECT COUNT(*) FROM l1_vec_rowids").fetchone()[0]
        db_ids = {r[0] for r in c.execute("SELECT record_id FROM l1_records")}
        c.close()
    except sqlite3.Error as e:
        sys.exit(f"vectors.db 读取失败: {e}")

    with open(rec_file, encoding="utf-8") as f:
        jl = [json.loads(line) for line in f if line.strip()]

    orphans = [r for r in jl if r["id"] not in db_ids]

    print(f"jsonl={len(jl)}  l1_records={n_rec}  l1_fts={n_fts}  向量条数(l1_vec_rowids)={n_vec}")
    print(f"孤儿记录(jsonl 有、向量库无): {len(orphans)}")
    for r in orphans:
        print(f"  {r['id']}  {r.get('content', '')[:60]}  session={r.get('sessionKey')}")

    if len(jl) != n_rec:
        print("⚠️  jsonl 与 l1_records 行数不一致 → 有在途/孤儿记录")
    if n_vec == 0:
        print("⚠️  向量条数 = 0 → 向量召回/去重空转(只剩 BM25)。注: l1_vec_chunks 是 chunk 元数据,别拿它判断有无向量。")


if __name__ == "__main__":
    main()
