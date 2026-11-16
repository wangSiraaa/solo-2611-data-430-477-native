# 454 · 整页采样 → 可合并工件 → HT 估计

这是 `materials/454/` 的完整数据采集链。旧的 `sampler.py` 直接把抽中页的行当独立样本并按行均值外推，设计期望有偏（seed 上 37.5 vs 真值 40）；本实现改为**以整页为抽样单元**，用页总额做 Horvitz–Thompson（HT）估计，并把设计、页总体、读取字节、样本行和覆盖状态都固化成可独立复查的工件。

```
plan  →  collect（每 worker 一个 part）  →  merge（按 job 选择清单核对）  →  estimate
                                                     ↘  inspect / list（重新打开也能复查）
```

## 设计（在看正文前冻结）

两种公开设计任选，均由目录（catalog）单独决定，`plan` 阶段不读任何页正文：

| 设计 | 选择方式 | 纳入概率 π | 联合纳入概率 π_ij |
|---|---|---|---|
| `wor` | 从 M 页简单随机**无放回**抽 k 页，2 ≤ k ≤ M | k/M | k(k−1)/(M(M−1)) |
| `bernoulli` | 每页以同一有理概率 p **独立**纳入，0 < p ≤ 1 | p | p² |

随机协议是自描述的 SHA-256，不依赖 Python `random` 状态：

* 页按 **page_id 的 UTF-8 字节序**排序。
* `wor`：对 `sha256-rank-v1 NUL seed NUL id` 取十六进制摘要作秩，选最小的 k 个（同秩按 id 字节序打破）。等价于均匀无放回抽样。
* `bernoulli`：对 `p=a/b`，用 `sha256-bernoulli-v1 NUL seed NUL id NUL counter` 的 256 位字做拒绝采样，得到 `[0,b)` 上的均匀整数，`< a` 即纳入；拒绝域 `u ≥ ⌊2^256/b⌋·b` 被丢弃，因此纳入概率**精确**为 a/b。

`plan` 工件记录：设计身份、源名与目录摘要、页总体（M、N、每页行数/摘要）、已选页、每页纳入概率、随机化过程（全部秩或全部伯努利抽样）以及 `plan_sha256`。重新打开时会**重放选择**并核对，选中列表与设计不一致的 plan 直接判为无效。

## 为什么按整页而不是按行

* 页大小和页内金额分布差异很大时，"抽中的行"并不是等概率的简单样本，行均值外推对页大小不均敏感；整页 HT 用每页总额一次，权重只取决于设计。
* 成本：一次 `Source.page` 读取整页，成本是页字节数而不是行数；按页抽样使读取成本与样本量都可直接计量（每个 part 都记录 `bytes`/`sha256`）。
* 方差：页是抽样单元，页间总额差异进入方差；无放回设计的有限总体校正 `(1−k/M)` 使 k=M 时方差恰为 0，Bernoulli 的 p=1 同理。行级 i.i.d. 方差会系统性低估这种页间差异。

## 工件与不变式

* **plan**：`page-sample-plan/1`。冻结设计、选择、页面总体。自摘要 `plan_sha256`。
* **part**（每 worker）：`page-sample-part/1`。含 `identity`（job_id、plan_sha256、源名、目录摘要）、`worker_id`、`requested`、`completed`、`failed`、真实 `pages`（每页 `n_rows`、`page_sha256` 为实际读取字节摘要、`content_sha256`、`total`、`rows`）以及 `reads`（实际 `Source.page` 调用记录）。自摘要 `part_sha256`。
* **sample**（merge 结果）：`page-sample/1`。按冻结选择清单核对；保留真实样本行；`coverage` 记录 selected/covered/missing；`status` 为 `complete` 或 `incomplete`；附带 `workers`、`duplicates`、`conflicts`、`issues`。自摘要 `sample_sha256`。

合并语义：

* **同页同内容重复投递**去重，只计一次，并在 `duplicates` 里显式写 `counted_as_new_sample: false`。
* **同页异内容**记为 `page_content_conflict`，该 job 为 `incomplete`，不估计。
* **缺页**（含 worker 读取失败且无他人补齐）记为 `missing_page`，`incomplete`，不估计。
* **跨 job / 跨源** part 的 identity 不匹配时整包拒绝（`foreign_part`），绝不合池。
* 损坏但仍有可核查页的 part：逐页校验，能核的页保留在样本里（可 `inspect` 逐行复查），但 job 仍为 `incomplete`，不能当 complete 估计。
* 不同 job 的完整工件只能通过 `list` 保留为**独立估计列表**，不能合并成一次无放回设计。

`merge` 的输出对 part 的传入顺序不敏感（workers/issues/duplicates 规范排序），便于比对与去重。

## 估计（`estimator.py`）

对已选页总额 `T_i`：

```
HT 总额      T̂ = Σ_{i∈s} T_i / π_i
设计方差     V = Σ_i (1−π_i)/π_i · T_i²  +  2 Σ_{i<j} (π_ij−π_iπ_j)/(π_iπ_j) · T_i T_j
无偏方差估计 v̂ = Σ_{i,j∈s} (π_ij−π_iπ_j)/π_ij · (T_i/π_i)(T_j/π_j),  π_ii=π_i
```

* `wor`：π=k/M，π_ij=k(k−1)/(M(M−1))；k=M 时 `v̂=0`。
* `bernoulli`：π=p，π_ij=p²；空选择时 `T̂=0`、`v̂=0`。
* 数值用 `fractions.Fraction` 精确计算，同时给出 18 位小数的半偶舍入显示；`numeric_policy` 明示误差口径（每个十进制字段与精确有理数相差不超过 5e-19，精确值以 `exact` 为准）。
* **空选择 = 估计 0、方差估计 0**，但报告明确说明这不等于总体为空或没有抽样误差。
* 只接受 `status=complete` 且通过工件校验（schema/摘要/覆盖）的样本；被篡改的"complete"工件会被拒绝估计。
* 不承诺某次样本估计接近真值，也不承诺区间必含真值（报告里没有把近似区间当保证）。

## 命令

在 `materials/454/` 下：

```bash
# 1) 冻结设计与选择（只读 catalog，不读页正文）
python3 cli.py plan --source seed --job demo --design wor --k 2 --seed "2026-11-16" --out plan.json

# 2) 两个 worker 分别采集选中的页（--page 可重复；不传则采全部已选页）
python3 cli.py collect --source seed --plan plan.json --worker w1 --page a --out w1.json
python3 cli.py collect --source seed --plan plan.json --worker w2 --page c --out w2.json

# 3) 乱序合并；重复投递可再加 --part；缺页/异内容会标 incomplete
python3 cli.py merge --plan plan.json --part w2.json --part w1.json --out sample.json

# 4) HT 总额与无偏方差估计
python3 cli.py estimate --sample sample.json

# 5) 重新打开工件独立复查（设计、覆盖、逐页可核查性）
python3 cli.py inspect --artifact sample.json
python3 cli.py inspect --artifact w1.json --artifact plan.json

# 不同 job 的工件保留为独立估计列表
python3 cli.py list --sample sampleA.json --sample sampleB.json
```

`Source.page` 每次调用都会按目录校验 SHA-256、JSON 与行数；`collect` 只允许读取 `plan["selected"]` 中的页，请求未选中页会在读取前被拒绝（CLI 的 `Source` 还额外设置了允许清单）。

## 验收与复现

```bash
python3 probe.py      # 复跑三页差异偏差 + 一次真实链
python3 verify.py     # oracle 枚举与真实链全量验收（独立读取计量）
```

`verify.py` 的 oracle 路径使用**独立的 `Source` 实例与独立读取日志**，与 job 的采集计量分开。它检查：

**三页 seed 偏差复现**（全部 3 对等概率 1/3）

| 页总额 | 真值 | 朴素行均值设计期望 | 偏差 | HT 设计期望 | 偏差 |
|---|---|---|---|---|---|
| a=0, b=10, c=30 | 40 | 75/2 = 37.5 | −5/2 | 40 | 0 |

**五页小总体**（`fixtures/small5`：a1=4, b2=12, c3=58, d4=60, **e0=0 空页**，总真值 134），枚举所选设计的**全部**结果及精确概率，独立核对"估计期望 = 真实总额""方差估计期望 = 设计方差"：

| 设计 | 支持大小 | E[T̂] | E[v̂] | 设计方差 V |
|---|---|---|---|---|
| wor k=2 | 10 | 134 | 6624 | 6624 |
| wor k=3 | 10 | 134 | 2944 | 2944 |
| wor k=5 (=M) | 1 | 134 | 0 | 0 |
| bernoulli p=1/2 | 32 | 134 | 7124 | 7124 |
| bernoulli p=1/3 | 32 | 134 | 14248 | 14248 |

概率质量均为 1；Bernoulli 空结果出现时 `T̂=v̂=0`（报告明确其含义）；空页 e0 始终是抽样单元（纳入概率非 0）。

**真实链检查**（`verify.py` 的 `real_chain`，全部为 `true`）：两 worker 实际读取；乱序合并摘要一致；逐行按 `row_id` 与 oracle 相同；同页同内容重复去重且不新增样本；缺页/异内容/损坏工件标 `incomplete` 且不可估计但可核查页保留；跨 job、跨源 part 被拒绝；不同 job 不合并；Bernoulli 任务（含空选择）complete 且 0/0；选择可确定性重放；持久化后重新打开可独立校验。

这些是设计与实现的期望检查，**不是**本次样本的质量承诺：某次估计不必接近真值，名义区间也不保证覆盖真值。

## 文件

| 文件 | 作用 |
|---|---|
| `util.py` | 规范 JSON/摘要、目录校验、行 schema（唯一 `row_id`、int64 `value`）、有理数/十进制口径 |
| `source.py` | 只读目录与**选中整页**的 `Source`；字节摘要、行数、schema 校验；读取日志 |
| `design.py` | 设计声明、SHA-256 随机协议、冻结选择、plan 生成与重放校验 |
| `collect.py` | worker 侧整页采集，生成 part 工件；拒绝未选中页 |
| `merge.py` | 按 job 核对覆盖、去重、冲突/缺页、跨 job/源拒绝、独立估计列表 |
| `estimator.py` | HT 总额与无偏设计方差估计；拒绝不完整/被篡改工件 |
| `artifact.py` | 原子保存、完整性校验加载、inspect、逐页 audit |
| `cli.py` | `plan` / `collect` / `merge` / `estimate` / `inspect` / `list` |
| `probe.py` | 三页偏差复现 + 一次真实链 |
| `verify.py` | oracle 枚举、偏差复现、真实链验收（独立计量） |
| `sampler.py` | 兼容导出（选择/采集实现已移入 `design.py` / `collect.py`） |
| `fixtures/small5/` | 五页小总体（含空页），用于枚举验收 |
| `seed/` | 原始三页冻结源（未改动） |
