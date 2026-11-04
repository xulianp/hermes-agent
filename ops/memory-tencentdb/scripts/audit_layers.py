#!/usr/bin/env python3
"""审计 memory-tencentdb 的 L0/L1/L2/L3 四层: 对账 + 污染关键词扫描。

只读,不修改任何数据。

用法:
  python3 audit_layers.py                      # 四层对账 + 各层规模
  python3 audit_layers.py --flags Go 深圳      # 额外扫描污染关键词
  python3 audit_layers.py --json               # 机器可读输出

退出码: 0 = 对账一致; 1 = 有差异(孤儿/缺向量); 2 = 数据目录缺失
"""
import argparse
import glob
import json
import os
import sqlite3
import sys

DATA = os.path.expanduser("~/.memory-tencentdb/memory-tdai")
DB = os.path.join(DATA, "vectors.db")


def read_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def all_records():
    recs = []
    for p in sorted(glob.glob(os.path.join(DATA, "records", "*.jsonl"))):
        recs.extend(read_jsonl(p))
    return recs


def all_conversations():
    recs = []
    for p in sorted(glob.glob(os.path.join(DATA, "conversations", "*.jsonl"))):
        recs.extend(read_jsonl(p))
    return recs


def record_key(rec):
    """L1 records 用 'id'; L0 conversations 的主键字段名未验证 —— 逐个候选找。"""
    for k in ("id", "conversation_id", "message_id", "record_id"):
        if rec.get(k):
            return k, rec[k]
    return None, None


def profile_dir():
    """找到 profiles/<tenant>/ 目录 (persona + scene_blocks 所在)。"""
    pats = glob.glob(os.path.join(DATA, "profiles", "*"))
    return pats[0] if pats else None


def id_column(cur, table):
    """主键列名在各表不统一: l1_records/l1_fts 用 record_id, l1_vec_rowids 用 id。"""
    try:
        cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return None
    for cand in ("record_id", "id"):
        if cand in cols:
            return cand
    return None


def table_count(cur, table):
    try:
        return cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return None


def table_ids(cur, table):
    col = id_column(cur, table)
    if not col:
        return None
    try:
        return {r[0] for r in cur.execute(f"SELECT {col} FROM {table}")}
    except sqlite3.Error:
        return None


def scan(text, flags):
    low = text.lower()
    return [f for f in flags if f.lower() in low]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--flags", nargs="*", default=[], help="污染关键词(区分大小写不敏感)")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    if not os.path.isdir(DATA):
        sys.exit(f"数据目录不存在: {DATA}")
    if not os.path.exists(DB):
        sys.exit(f"检索库不存在: {DB}")

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = con.cursor()

    l1_jsonl = all_records()
    l0_jsonl = all_conversations()
    l1_db = table_ids(cur, "l1_records") or set()
    l1_fts = table_ids(cur, "l1_fts") or set()
    l1_vec = table_ids(cur, "l1_vec_rowids") or set()
    l0_db_n = table_count(cur, "l0_conversations")
    l0_fts_n = table_count(cur, "l0_fts")
    l0_vec_n = table_count(cur, "l0_vec_rowids")

    jl1 = {r.get("id") for r in l1_jsonl if r.get("id")}
    # L0: 不假设主键字段名 —— 先探测, 探测不到就只报行数
    l0_key, l0_ids = None, set()
    for r in l0_jsonl:
        k, v = record_key(r)
        if k:
            l0_key = k
            l0_ids.add(v)
    jl0 = l0_ids if l0_key else set()

    prof = profile_dir()
    scene_dir = os.path.join(prof, "scene_blocks") if prof else None
    scenes = sorted(glob.glob(os.path.join(scene_dir, "*.md"))) if scene_dir else []
    persona = os.path.join(prof, "persona.md") if prof else None

    orphan_l1 = jl1 - l1_db
    missing_vec = l1_db - l1_vec
    fts_gap = l1_db - l1_fts

    report = {
        "l0": {
            "jsonl_lines": len(l0_jsonl),
            "jsonl_ids": len(jl0),
            "jsonl_id_field": l0_key,
            "db": l0_db_n,
            "fts": l0_fts_n,
            "vectors": l0_vec_n,
        },
        "l1": {
            "jsonl": len(jl1),
            "db": len(l1_db),
            "fts": len(l1_fts),
            "vectors": len(l1_vec),
            "orphans": sorted(orphan_l1),
            "missing_vector": sorted(missing_vec),
            "fts_gap": sorted(fts_gap),
        },
        "l2": {"scene_blocks": len(scenes)},
        "l3": {"persona_exists": bool(persona and os.path.exists(persona))},
    }

    if args.flags:
        hits = {"l1": [], "l2": [], "l3": []}
        for r in l1_jsonl:
            f = scan(str(r.get("content", "")), args.flags)
            if f:
                hits["l1"].append({"id": r.get("id"), "flags": f, "text": str(r.get("content", ""))[:120]})
        for p in scenes:
            s = open(p, encoding="utf-8").read()
            f = scan(s, args.flags)
            if f:
                hits["l2"].append({"file": os.path.basename(p), "flags": f, "bytes": len(s)})
        if persona and os.path.exists(persona):
            s = open(persona, encoding="utf-8").read()
            f = scan(s, args.flags)
            if f:
                hits["l3"].append({"file": "persona.md", "flags": f, "bytes": len(s)})
        report["flag_hits"] = hits

    # persona 是每轮无条件注入的 —— 它的命中权重最高
    if args.flags and report["flag_hits"]["l3"]:
        report["flag_hits"]["_note"] = (
            "L3 persona 命中 = 每轮无条件注入,优先级最高; "
            "L2 命中可能只是场景块提及; L1 命中需人工判读真伪"
        )

    con.close()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("═══ 四层对账 ═══")
        l0, l1 = report["l0"], report["l1"]
        print(f"  L0  jsonl行={l0['jsonl_lines']}  jsonl唯一id={l0['jsonl_ids']}(字段={l0['jsonl_id_field']})  db={l0['db']}  fts={l0['fts']}  vec={l0['vectors']}")
        print(f"  L1  jsonl={l1['jsonl']}  db={l1['db']}  fts={l1['fts']}  vec={l1['vectors']}")
        print(f"  L2  场景块 = {report['l2']['scene_blocks']}")
        print(f"  L3  persona.md 存在 = {report['l3']['persona_exists']}")
        print(f"\n  L1 孤儿(jsonl有/db无): {len(l1['orphans'])} {l1['orphans']}")
        print(f"  L1 缺向量(db有/vec无): {len(l1['missing_vector'])} {l1['missing_vector']}")
        print(f"  L1 FTS缺口(db有/fts无): {len(l1['fts_gap'])} {l1['fts_gap']}")

        if args.flags:
            fh = report["flag_hits"]
            print(f"\n═══ 污染关键词扫描 {args.flags} ═══")
            for layer, key in (("L1", "l1"), ("L2", "l2"), ("L3", "l3")):
                print(f"  --- {layer}: {len(fh[key])} 处 ---")
                for h in fh[key]:
                    ident = h.get("id") or h.get("file")
                    extra = f" [{h.get('bytes')}B]" if "bytes" in h else ""
                    print(f"    {ident}  命中{h['flags']}{extra}")
                    if "text" in h:
                        print(f"      {h['text']}")
            if fh.get("_note"):
                print(f"\n  ⚠️  {fh['_note']}")

    diff = bool(orphan_l1 or missing_vec or fts_gap)
    sys.exit(1 if diff else 0)


if __name__ == "__main__":
    main()
