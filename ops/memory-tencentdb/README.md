# memory-tencentdb 第二记忆模块 — 部署与运维说明

> 建立日期: 2026-11-03
> 上游代码: `~/program/TencentDB-Agent-Memory/MemoryCore`
> 本机 Hermes: `~/.hermes/hermes-agent` (通用插件 `plugins/memory_tencentdb/` 随仓库加载)

---

## 1. 架构与工作方式

```
Hermes Agent (Python) — all surfaces: CLI / TUI / gateway / ACP
  └─ plugins/memory_tencentdb  (general plugin, turn hooks)
       ├─ pre_llm_call   → 召回 L1+L3+L2, 注入当前 user message
       └─ post_llm_call  → 本轮 transcript → L0 (/v3/conversation/add)
            └─ HTTP 127.0.0.1:8420
         memory-tencentdb Gateway (Node.js)
          └─ Core 四层:
               L0 Conversation store  (SQLite + JSONL)
               L1 Episodic extraction (LLM + vector dedup)
               L2 Scene blocks        (Markdown)
               L3 Persona synthesis   (persona.md)

  + local-embed.py (127.0.0.1:8421)      ← bge-small-zh-v1.5, OpenAI 兼容
  + llm-proxy.py   (127.0.0.1:8422)      ← MiniMax thinking / work_task status 规范化
```

**与 builtin 并存**: builtin MEMORY/USER 仍由 MemoryManager 管理；tencentdb 走
通用的 `pre/post_llm_call` 钩子，不占用 `memory.provider`（一次只能一个）。
两者职责分离:

| | builtin MEMORY/USER | tencentdb L0-L3 |
|---|---|---|
| 注入方式 | 每轮全量 | 按需召回 (BM25 + 向量) |
| 适合装 | 每轮都要用的极短规则 | 海量、低频、细节多的项目知识 |
| 写入 | agent 手动整理 | 自动抽取 + 去重 |
| 容量 | 2200 / 1375 字符硬上限 | 无字符上限 (磁盘) |

---

## 2. 目录与文件

| 路径 | 作用 |
|---|---|
| `~/.hermes/memory-tencentdb/tdai-gateway.yaml` | Gateway 配置 (LLM/存储/pipeline/recall) |
| `~/.hermes/memory-tencentdb/start-gateway.sh` | Gateway 启动脚本 (注入凭证) |
| `~/.hermes/memory-tencentdb/local-embed.py` | 本地 embedding 服务 (bge-small-zh-v1.5) |
| `~/.hermes/memory-tencentdb/llm-proxy.py` | MiniMax L1 抽取/状态词表代理 |
| `~/.hermes/memory-tencentdb/supervise.sh` | **start / stop / restart / status** |
| `~/.hermes/memory-tencentdb/{embed,llm-proxy,gateway,supervise}.log` | 运行日志 |
| `~/.memory-tencentdb/memory-tdai/` | 数据目录 (L0 jsonl / L1 records / L2 scene_blocks / L3 persona.md) |
| `~/.hermes/hermes-agent/plugins/memory_tencentdb/` | Hermes 通用插件（消息链路集成） |

---

## 3. 运维命令

```bash
hermes memory-tencentdb status    # 查看三个组件
hermes memory-tencentdb start     # 启动 (embed → llm-proxy → Gateway)
hermes memory-tencentdb restart   # 重启
hermes memory-tencentdb stop      # 停止
```

插件在 `on_session_start` 时确保三个组件在跑（文件锁防并发），但**不在会话结束时停止它们**；
`supervise.sh` 仍是唯一的进程所有者。也可以直接调 `supervise.sh`。

### 3.1 systemd 用户单元（必须装，否则 gateway 重启会顺手杀掉 sidecar）

`hermes-gateway.service` 的 `ExecStopPost` 会跑 `gateway.cgroup_cleanup`，把**自己 cgroup 里的所有子进程**
一起清掉。插件原本直接在 gateway 进程里 `nohup` sidecar，于是每次 `hermes gateway restart`
三个 sidecar 一起死，重启后的第一轮消息会撞上 `Connection refused`，L0 直接丢一轮。

修法：sidecar 跑在自己的用户单元里（`Type=oneshot` + `RemainAfterExit`，进程被留在该单元 cgroup）：

```bash
cp ~/.hermes/hermes-agent/ops/memory-tencentdb/memory-tencentdb.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now memory-tencentdb.service
```

- 单元存在时，插件与 `hermes memory-tencentdb {start|stop|restart}` 都走 `systemctl --user`；
  单元不存在时自动退回直接跑 `supervise.sh`（可用，但会重新落回 gateway cgroup）。
- 验证：三个 pid 的 `1:name=systemd` 必须是 `…/memory-tencentdb.service`，
  而 gateway 的在 `…/hermes-gateway.service`。

```bash
for p in $(pgrep -f "local-embed.py|llm-proxy.py|src/gateway/server.ts"); do
  echo "$p $(grep '^1:name=systemd' /proc/$p/cgroup)"; done
```

### 3.2 sidecar 的环境必须干净

- `supervise.sh` 开头 `unset PYTHONPATH PYTHONHOME ...`：Hermes 在 gateway 进程里注入的是 **3.14 的
  site-packages**，3.11 的 embed venv 继承了它 → `import huggingface_hub` 命中 3.14 的那份 →
  `cannot import name 'ResolvedRevision'` → **embedding 一定报 500，向量去重/检索静默退回 BM25**。
- `start-gateway.sh` 在 `PATH` 里找不到 `node` 时会回退到 `~/.hermes/tools/node-*/bin`：
  systemd 用户单元的默认 `PATH` 不含 Hermes 自带的 node。

---

## 4. 关键配置决策 (实测得出, 别乱改)

### 4.1 LLM 端点必须用 `/v1` 而不是 `/anthropic`
Hermes 自己用 MiniMax 走 `anthropic_messages` 协议 (`/anthropic` 端点),
而 Gateway 用 OpenAI SDK → **必须指向 OpenAI 兼容的 `https://api.minimaxi.com/v1`**。
实测: `/v1` → HTTP 200 + 正常 content。

### 4.2 `everyNConversations: 1`
插件在 `post_llm_call` 每轮发**本轮增量**（至少 1 条 user 消息）→ `rounds=1`。
设 5 (默认) 或 3 都要等好几轮才抽取。设 1 = 每轮即抽取。
兜底: `l1IdleTimeoutSeconds: 60` (空闲 60s 也触发)。
⚠️ 只发 user/assistant 文本；`tool`/`system` 行被丢弃（Gateway schema 不接受）。

### 4.3 embedding 必须开 (否则 dedup 失效)
`embedding.provider: none` 时, L1 dedup 只剩 **FTS5 BM25** 降级路径 →
拦不住"同一事实的不同措辞"。**实测**: 3 条语义相同的记忆 → BM25 下 3 条全放行;
开向量后 → **只存 1 条**。

### 4.4 embedding 用本地服务 (旧 MiniMax shim 已停用)
当前 8421 是 `local-embed.py`（BAAI/bge-small-zh-v1.5, **512 维**），OpenAI 兼容。
`embedding-shim.py` 只作为 MiniMax 协议回退参考保留，不再被 `supervise.sh` 调用。
换模型 = 换维度 = 重建向量表；旧 L1 记录不会自动重嵌入，需跑
`ops/memory-tencentdb/scripts/reindex-local.ts`。详见 `references/local-embedding.md`。

---

## 5. 实测数据

| 项 | 结果 |
|---|---|
| Gateway health | `{"status":"ok","stores":{"vectorStore":true,"embeddingService":true}}` |
| L1 抽取延迟 | ~15s (everyNConversations=1) |
| L1 抽取质量 | "以后所有技术方案都用中文输出, 先给结论再给推导" → 抽成 `work_method` ✅ |
| L2 场景块 | ✅ 产出 ("后端协作-Go与PostgreSQL技术栈") |
| 向量 dedup | 3 条同义 → 存 1 条 ✅ |
| embedding shim | 同义句余弦 1.0000, 无关句 0.0043 |
| 注入量 | 清理重复前 3365 字符 → 后 **589 字符** (省 82%) |

---

## 6. 已知限制与坑

1. ⚠️ **L1 对长文本会整条判空** —— 迁移项目细节时, 一条消息塞太多内容 → LLM 判"无有效记忆" → 什么都不存。
   **解法: 拆成短消息, 一条一个事实** (实测: 长消息 0 抽取, 拆成 3 条短消息全部抽出).
2. ⚠️ **embed 必须在 Gateway 之前起** —— Gateway 启动时若 embedding 不可用, 后续请求会失败.
   `supervise.sh start` 已按 embed → llm-proxy → Gateway 顺序执行.
3. ⚠️ **L2 有 90s 延迟** (`l2DelayAfterL1Seconds`) → 场景块不会立刻出现.
4. ⚠️ **Gateway 进程 ~550MB RSS** —— 本机 8GB, 跑重实验时注意.
5. ⚠️ **首轮可能召不回** —— 组件是懒启动: `on_session_start` 在后台拉起，第一轮
   `pre_llm_call` 只等 3 秒；未就绪时本轮跳过召回（不会阻塞对话）。
6. ⚠️ **`codex_app_server` 通道不经过 turn 钩子** —— 该模式绕开 finalizer，
   既不写入 L0 也不注入召回；聊天补全 / CLI / TUI / gateway 均正常。
   被中断或空回复的轮次也不会触发 `post_llm_call`，同样不写入 L0。
7. ⚠️ **租户键没有 profile 维度** —— 同一平台 `user_id` 在不同 profile 间共享
   L0-L3；需要隔离时只能按用户/团队拆 Gateway 实例。
8. ⚠️ **sidecar 是机器级常驻进程** —— 固定端口 8420-8422、运行时目录属于
   进程启动 home；`hermes -p <profile>` 不会另起一套。Windows 无 bash/fcntl
   时插件可加载但无法自启动 sidecar。

---

## 7. 回滚

```bash
hermes plugins disable memory_tencentdb
hermes memory-tencentdb stop
# 然后重启 hermes gateway
```
builtin MEMORY.md / USER.md 一直在工作, 回滚不影响任何原有记忆；L0-L3 数据留在
`~/.memory-tencentdb/memory-tdai/`，重新启用后继续使用。

---

## 8. builtin 精简记录 (2026-11-03)

精简前: memory **2143/2200 (97% 满)**, user 1373/1375 (100% 满), 注入 3816 字符.
精简后: memory **923/2200 (42%)**, 注入 2594 字符 (省 1222).

迁出到 tencentdb 的内容 (已存为可召回的 L1 记忆, 不是丢弃):
- 内部评测项目的常驻事实与实验结论
- 内部论文的落地状态
- TencentDB-Agent-Memory 仓库与配置路径

留在 builtin 的 7 条 (每轮都要用 / 环境事实 / 用户偏好):
Skill owner · 强制发图 · 硬件 · 自动化偏好 · 评测项目指针 · gateway 排障 · 教学规则

⚠️ **USER.md 仍 100% 满** —— 它装的全是用户偏好 (最该常驻), 但没有空间了.
下一步应把其中的"验证脚本自清理"等条目与 memory 合并, 或提额 `user_char_limit`.
