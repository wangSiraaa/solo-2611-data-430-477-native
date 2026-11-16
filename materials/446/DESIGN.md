# 446 有序聚合状态：设计说明

## 1. 问题与旧实现的缺陷

旧实现把 `STRING_AGG` 渲染出的字符串当作可合并状态，存在三个本质缺陷：

1. **状态不可逆**：`partial` 直接产出 `|` 拼接文本，相同 value 的不同 id、
   后续 distinct、字节级 limit 都无法从字符串还原；
2. **无分片身份与冲突校验**：池用 `INSERT OR REPLACE`，同 chunk 异内容、
   跨 chunk id 重复都被静默覆盖/重复计数；
3. **顺序依赖到达次序**：`finish` 按池中行序 `extend`，换分片/换到达次序
   结果就变；截断按字节切（切断多字节字符），并把字节差当作 omitted 数量。

本实现的核心原则：**交换的是结构化、有序、可重开的状态，而不是已渲染文本**；
partial 必须真实读取原件，reducer 永不重新打开原件。

## 2. 状态格式（`*.state`，跨进程交换单元）

全部整数小端序。文件布局：

```
MAGIC  b"ORDAG1\n"
header 一行 UTF-8 JSON（含 64 位定宽 digest 槽，长度恒定，可原地回填）
record frames —— 已按组内排序键全局有序
id section    —— u64 计数 + 每个唯一 id 一个 s64，升序
footer        —— u32 header_len | u32 records_len | u32 ids_len
                 | u32 record_count | b"ORDFOOT1"   （共 24 字节）
```

record frame 同时也是参与摘要的**规范字节**（canonical frame）：

```
u16 group_bytes | group UTF-8 | u8 null_flag | s64 key(=0 when NULL)
| s64 id | u16 value_bytes | value UTF-8
```

- value 最长 256 字节、group 最长 32 字节，长度字段用 u16 足够；
- `digest = sha256(按排序键有序的全部 canonical frame)`，与片内行到达
  顺序无关，因此同内容重送产生逐字节相同的状态；
- 状态内**不出现任何渲染字符串**，只存 (group, key, id, value) 出现，
  相同文本不同 id 是不同出现，信息无损。

### 排序语义（所有阶段统一）

组内：`key` 升序、`NULL` 最后、再 `id` 升序；组间按 group 文本升序。
合并多个已排序状态时用稳定的 k 路归并（`heapq.merge`，键为
`(group, key is None, key, id)`）。因此**不同到达顺序、不同分片划分、
多层合并树都得到同一最终顺序**——这由归并的数学性质保证，而非靠重排输入。

## 3. 三个阶段

### partial（`ordered_state.py`）

真实读取原件（JSON list 或 JSONL 流），逐行校验四字段与约束
（group 非空、key 为 int64 或 null、id 为 int64 且片内唯一、value 非空
且无 `|`）。编码后的 frame 仅在内存缓冲到 `spill_bytes`（默认 32 KiB），
超限即落外部排序 run（`.rec` 记录 + `.ids` 有序 id 伴随文件）；最终用流式
k 路归并生成 state，活跃载荷有界。小数据走纯内存单源路径，无临时 I/O。

输出 chunk 描述符（manifest 条目）：`chunk / digest / 有序 ids / 计数`。

### 外存聚合池（`run_pool.py`）

池目录 = `catalog.sqlite` + `states/<chunk>.state`。SQLite 以
`page_size=4096, cache_size=-16` 打开，**页缓存上限 16 KiB，单独报告，
不计入 128 KiB 载荷预算**；临时排序走文件（`temp_store=FILE`）。

准入契约（事务内、全有或全无；准入前先做 `verify_state` 全量自校验——
从实际 record frame 重算 sha256 并与 header digest 比对、校验 id 段，
因此即使 header 被保留而帧被篡改也会被拒绝）：

| 情况 | 处理 |
|---|---|
| 同 chunk + 同 digest + 同 id 集 | 幂等，不重复计数 |
| 同 chunk、内容不同 | `ChunkConflict` 拒绝 |
| id 跨不同 chunk 重复 | `OverlapConflict` 拒绝（索引支撑的流式查重） |
| manifest 的 digest/id 集与 state 不符或缺 chunk | `ManifestConflict` 拒绝 |
| 非本格式/损坏文件 | 读 header/footer 即抛错 |

manifest 固定 chunk 身份、互不重叠 id 集与原件摘要；它属于题面豁免的
“最多 32 片 manifest”。state 原文复制进池后即不再依赖来源路径，可被任意
进程重新打开；`merge_pool` 支持池对池的多层合并。

### finalize（`finalizer.py`）

池的稳定归并产出全局有序记录，finalize 以**单记录前瞻、逐记录流式**处理，
任何时刻都不物化整组（避免大组直接撑爆预算），逐组流出、不保留全部输出。
每条解码记录按 `deep_size` 实时计入 `input_record` 并在用完即释放。

- 普通模式：保留全部出现；
- distinct：按 value **完全相等**保留全局顺序中的第一次；
- 分隔符固定 `|`，不出现在首尾；
- limit（1..4096 UTF-8 字节）：从排好且已 distinct 的值序列取**最长完整值
  前缀**，串接字节 ≤ limit；某值放不下即停，**不越过它**继续塞后面的值；
- 单值本身 > limit：该组文本为空，所有有效出现计 omitted；
- omitted 是**未输出的值出现数**（distinct 时是未输出的不同值数），
  绝不把字节数当数量。

distinct 的去重工作集有界：在线集合超过 `dedup_cap_bytes`（默认 32 KiB）
即切外部路径——把 value frame 溢写、受限内存外部排序+相邻去重得到有序
唯一文件，再从池重放该组、以流式成员判定重建首次出现顺序。整个过程
驻留载荷不随组大小增长。

## 4. 资源口径（`meter.py`，真实运行记录而非声明）

- **活跃载荷**按类别 add/release 统计驻留字节并维护峰值，超 128 KiB 直接
  抛 `BudgetExceeded`（演示中峰值约 64 KiB）；
- 同一份数据多份驻留分别计入（缓冲、归并、去重集、构造中的输出各自有类目）；
- 解释器、路径、≤32 片 manifest 豁免；SQLite 16 KiB 页缓存单列；
- 磁盘上的 state/run 不算驻留，但**读写流量逐字节累计**
  （`temp_run / state / state_copy / dedup_spill / sqlite_pages`）；
- 每次运行写 JSONL meter 日志，`inspect.py meter` 可复核峰值、溢写事件与
  流量；最终结果逐组流式落 JSONL，不要求保留全部输出。

## 5. 备选布局取舍（`sqlite_layout.py`，实测于 `run_demo.py`）

备选“全 SQLite”布局把每条记录的列存入表，用 `UNIQUE(id)` 在引擎内强制
跨片去重、用 `(grp,key_null,keyv,id)` 索引提供有序扫描，distinct 用引擎
`COUNT(DISTINCT)`。演示数据集上的实测对比（见 `demo-work/report.json`）：

| 维度 | 有序 run + 薄 catalog（本实现） | 全 SQLite |
|---|---|---|
| 磁盘写 | 追加 state 文件 + 归并读，写入约等于状态体量 | 额外的索引/页/回滚写，文件增长约 1 MB |
| 去重 | Python 有界集合，超限走外部溢写文件，机制透明可控 | 引擎内排序去重，16 KiB 缓存下由引擎自身溢盘 |
| 首结果等待 | 已排序文件直接流式归并，亚毫秒~毫秒级出首行 | 依赖引擎排序/索引计划，首行更快但受统计信息影响 |
| 可移植性 | 语言中立二进制 state，任意进程无数据库即可读 | 单文件自洽，但绑定 SQLite 才能消费 |
| 冲突语义 | 在 catalog 显式实现，四类拒绝可读可测 | 部分交给约束（UNIQUE），同 chunk 异内容仍需应用层判 digest |

结论：全 SQLite 在去重与首行延迟上有引擎优势，但写入放大、可移植性与
语义可见性较弱；本实现选择有序 run，使状态可跨进程裸交换、合并顺序可证明、
资源占用可逐字节解释。

## 6. 入口与复现

- 库入口：`ordered_state.partial`、`run_pool.Pool(.merge/.merge_pool/.add_state)`、
  `finalizer.finalize`、`inspect`（`inspect.py`）；
- CLI：`ordered_state.py`、`run_pool.py {merge,merge-pool,inspect}`、
  `finalizer.py`、`inspect.py {state,pool,meter}`；
- `probe.py`：新系统正常单片/分片 + 旧实现三类错误对照；
- `test_aggregation.py`：22 项，含**独立 OS 进程**生成状态、换到达次序/
  分片/合并树、独立全排序 `oracle.py` 核对、重复文本跨片、NULL/同 key、
  空片、中文恰好装下/差一字节、四类冲突重送、外部 distinct 溢写与预算强制；
- `run_demo.py`：4096 个不同值跨 8 片（含 1 空片）交错的公开溢写演示，
  必须成功；产物在 `demo-work/`，资源记录在 `demo-work/logs/`。

```
python3 probe.py
python3 -m unittest test_aggregation -v
python3 run_demo.py
```
