# 跨层污染审计 (L1 → L2 → L3)

多层记忆的写入不是"写一条记录",而是**种一条会自己往上长的东西**:L0 的一行 → L1 的一条情节 → L2 的一个场景块 → L3 persona 里的一条规则。顶层(persona)**每轮无条件注入**,所以一次测试语料的代价不是一行,而是一条常驻的行为约束。

审计要**逐层独立查**,再从**召回侧**验一次。只查 L1 会漏掉真正伤到用户的那条。

---

## 1. 污染是怎么扩散的

```
测试语料 → L0 写入
   ↓ L1 抽取(LLM 把"事实"抽成记忆)
L1 记忆 —— 假事实在这里成形
   ↓ L2 聚合(多个记忆聚成场景块)
L2 场景块 —— 假事实被扩写成"工作场景/适用条件/判断逻辑"
   ↓ L3 合成(persona 蒸馏成规则)
L3 persona —— 假事实变成"Agent 默认使用 X"这种指令级条目,每轮注入
```

关键性质:**越往上,假内容的表述越像真的**。L1 是一句可证伪的话("成员使用 Go 和 PostgreSQL"),L2 是一整段带"核心 SOP / 判断逻辑 / 禁忌"的文档,L3 是一条不带主语的祈使句("Agent 默认使用既定栈")。人工扫一眼 L1 觉得"这明显是测试数据",看到 L3 就已经像一条正常规则了。

---

## 2. 审计流程

### 2.1 先确认当前数据状态(不要信 health,不要信 checkpoint)

```bash
python3 scripts/audit_layers.py            # 四层对账 + 分层清单
```

输出应包含:每层的 jsonl 行数 / db 表行数 / fts 行数 / 向量条数,以及差额。差额就是孤儿 —— 写了但召不回。

### 2.2 逐层扫描污染关键词

给 `scripts/audit_layers.py` 传你怀疑的测试语料关键词:

```bash
python3 scripts/audit_layers.py --flags Go PostgreSQL 深圳
```

脚本会在 L1 记录、L2 场景块、L3 persona 三处分别列出命中行。

**人工判读时区分三类,不要一律当污染删:**

| 类别 | 特征 | 处理 |
|---|---|---|
| 假事实 | 直接断言了编造内容("成员使用 Go 和 PostgreSQL") | **删** |
| 提及污染的记录 | 复述了污染事件("测试曾用 Go 编造数据") | **保留** —— 这是有价值的元记忆 |
| 真实但碰巧同词 | 真实讨论 Go/PostgreSQL | **保留** |

判定依据是**这条记录断言了什么**,不是**它包含哪些词**。按关键词盲删会把元记忆和真实内容一起清掉。

### 2.3 从召回侧验一次

存了不等于会被召回,也不等于会被注入。两条路径都要验:

```bash
# a) 检索路径: 用污染关键词直接搜,看假事实能否被召回
curl -sS -X POST http://127.0.0.1:8420/v3/atomic/search \
  -H 'Content-Type: application/json' \
  -H 'x-tdai-service-id: default' \
  -d '{"team_id":"default","agent_id":"default","user_id":"default",
       "query":"后端开发团队用什么技术栈","limit":5}'

# b) 注入路径: 看 persona 全文 —— 它每轮都进 prompt,没有召回门槛
cat ~/.memory-tencentdb/memory-tdai/profiles/*/persona.md
```

检索路径是"相关时才会伤到人",注入路径是"每轮都伤"。**注入路径的优先级更高。**

### 2.4 量一下每轮注入成本

污染清理的收益要用注入量衡量,不是用记录条数:

```
每轮注入 ≈ len(persona.md) + len(场景索引) + len(本轮召回的 <relevant-memories>)
```

实测这个量级远小于把整个库塞进 prompt 的做法,所以"召回型后端 + 双通道并存"的代价比预期小 —— 前提是**顶层干净**。

---

## 3. 清理

### 顺序(不能反)

```
1. 删污染 L1 记录        ← 先删源头
2. 删/改污染 L2 场景块
3. 重建 L3 persona (正文 + 场景导航段)
4. 验: 新 persona 不含污染关键词
```

**为什么必须先删 L1 再重建 persona**:persona 是从 L1/L2 派生的。如果 L2 里还留着污染场景块就重新生成 persona,新 persona 会把同样的污染重新蒸馏一遍 —— 清理变成空操作。

### L2/L3 的读写必须走 API, 别直接改磁盘文件

L2 场景块和 L3 persona 都由 Gateway 维护**索引 + 向量库副本 + 导航段**三处联动。
直接用编辑器改 `scene_blocks/*.md` 或 `persona.md` 只改到其中一处 → 索引里的 summary/heat
与文件内容不一致, 导航段仍指向旧内容, 下次 pipeline 跑完还会把手改内容覆盖回去。

**统一请求头** (所有 `/v3/*` 端点都要):

```bash
-H 'Content-Type: application/json' \
-H 'x-tdai-service-id: default' \
-H 'Authorization: Bearer local'
```

**统一请求体三元组** (决定数据落在哪个 `profiles/<scope>/`):

```json
{"team_id": "default", "agent_id": "default", "user_id": "default"}
```

| 端点 | 请求体 | 作用 |
|---|---|---|
| `POST /v3/scenario/ls` | 三元组 | 列场景块 → `data.entries[]` (`path` / `summary` / `version`) |
| `POST /v3/scenario/read` | 三元组 + `path` | 读单个场景块全文 (`data.content`, 含 META 头) |
| `POST /v3/scenario/write` | 三元组 + `path` + `content` (+ 可选 `summary`) | 覆写场景块, 同步刷新 `scene_index.json` |
| `POST /v3/scenario/rm` | 三元组 + `path` | 删场景块 + 索引条目 |
| `POST /v3/core/read` | 三元组 | 读 persona 正文 (含导航段, 因为它是文件原样) |
| `POST /v3/core/write` | 三元组 + `content` | 写 persona —— ⚠️ **写入前会 strip 掉导航段** |

⚠️ **`/v3/core/write` 不能用来写导航段** —— 它内部调 `stripSceneNavigation(content)` 后才落盘,
所以你拼进去的导航会被剥掉, 写完只剩正文。导航段是**派生内容**, 有两个正当来源:

1. **让 pipeline 重建**: 跑一次 L2 (完成后会调 `updateSceneNavigation` 追加新导航), 或触发 profile 同步
   (`refreshPersonaNavigation`)。
2. **自己按上游函数生成**: 用 `readSceneIndex(dataDir)` + `generateSceneNavigation(index)` 拼到正文后写盘。
   一次性脚本要放在上游仓库的 `src/` 下才能用相对路径 import 上游模块 (`node --import tsx` 直接跑),
   **用完即删**; 改上游仓库会让 `git status` 变脏。

⚠️ **数据目录是按 scope 分层的, 别只看顶层**:
场景块和 persona 在 `profiles/<percent-encoded-scope>/` 下 (例如
`profiles/team%3Adefault%7Cagent%3Adefault/scene_blocks/`), 顶层 `scene_blocks/` 可能是空目录。
**用 `ls` 找到真实 scope 目录再统计**, 否则会得出"L2 是空的"这种错误结论 —— 顶层空 ≠ 没有场景块。

⚠️ **`/v3/atomic/delete` 删 L1 时不同步 `records/*.jsonl`** —— 三路索引都少了, jsonl 原样保留。
删完必须手工同步 jsonl, 否则污染源仍在磁盘 (重启可能重新入库), 且对账脚本会一直报假孤儿。

### ⚠️ pipeline 会在你清理的同时异步改动同一批产物

L2 提取和 L3 persona 生成都挂在**墙钟定时器**上(`l2DelayAfterL1Seconds` /
`l2MinIntervalSeconds` / `l3IdleTimeout`),不跟你的对话同步。实测一次清理中,
pipeline 在我删 L1 之后、验 persona 之前**自己重写了那个污染场景块**(把它改写成
"污染登记块"并加了溯源说明),又**自己重建了一次 persona**(`last_persona_time`
推进到清理动作之后)。后果:

- 你据以决策的"当前内容"可能已经不是磁盘上的内容 → 删一个你以为还在的块,
  实际删的是它已被改写后的版本
- 你验证通过之后, pipeline 又跑一轮 → 验证结论立刻过期
- 一次"重建 persona"后 `last_persona_time` 变了, 你无法区分是你写的还是 pipeline 写的

**做法**:
1. **动手前**记下 `checkpoint.json` 的 `last_persona_time` / `scenes_processed` /
   `memories_since_last_persona`, 作为"pipeline 是否动过"的判据。
2. **每次读一个产物前先读一遍它**(不要缓存上一轮的读结果)—— 清理过程中产物可能已变。
3. **删除/改写前后各读一次目标**, 确认你操作的仍是同一个版本; 若内容已变, 重新判读。
4. **验 persona 之前先看 `last_persona_time` 是否比你写入的时刻新**。若是, 说明
   pipeline 重建过, 你的手写内容可能已被覆盖 → 重新读 persona 全文再判。
5. 需要"清完后 pipeline 不要再来一轮"时, 暂停 pipeline 或在验证通过后**再复验一次**
   (等一个 L2 周期), 不要以单次验证作为"清理完成"的证据。

**反过来利用它**: pipeline 会自动给 L2 场景块补"演化记录 / 来源待溯源"的免责说明 ——
这解释了为什么清理前先看到 L2 已自我标注、L3 却毫无标注(顶层不参与该链路)。

### persona 重建后必须验

重建后重新 `cat persona.md` 搜污染关键词。**不要假设重建会清干净** —— 顶层可能保留旧条目,或把 L2 里残留的污染重新聚合进去。验不过就说明第 2 步没做干净,回到第 2 步。

### 清理后回归

- 再跑一次 `scripts/audit_layers.py --flags ...` —— 命中应只剩"提及污染的记录"和真实同词
- 召回侧重跑 2.3 的 curl,确认假事实不再被召回
- builtin 通道没被影响:`memory` / `user` 字符数与清理前一致

---

## 4. 预防(比清理便宜得多)

**写入测试语料必须满足其一:**

- **可一次性识别并清除** —— 带明显标记,清理时按标记批量删,不用逐条判读
- **先定好下游清理范围** —— 写之前就知道会污染哪些层、要清哪些文件

**绝不能用真实用户事实当测试语料。** 真实事实(用户的技术栈、团队规范)一旦被提炼进 persona,会以最高优先级留在顶层,比任何临时性错误都难清 —— 因为它读起来是真的。

**顶层产物(L2 场景块、persona)不参与"自动免责标注"的完整链路。** 实测 L2 场景块会被补上"演化记录 / 来源待溯源核对"的说明,而 L3 persona 里对应的假规则**不会**被标注、照常注入。所以别把"场景块已经打了标"当作"顶层干净了"。

**写入测试与生产用不同的 team/agent 租户**,能把污染完全隔离在另一个 `profiles/<tenant>/` 目录里,清理即删目录。
