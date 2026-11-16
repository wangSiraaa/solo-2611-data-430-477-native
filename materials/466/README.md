# Question 466 — 追加日志的跨检查点前缀一致性证明

原原型只证明“第一片叶仍存在”，因此替换其它旧叶也会被接受（运行旧 `probe.py`
可见 `changed_old_prefix_accepted: true`）。本目录给出完整实现：见证仅凭其已信任
的 `(old_size, old_root)`、新头 `(new_size, new_root)` 和一致性证明，就能在没有
日志原件的情况下，密码学确认**整个旧前缀原序不变**。

## 密码学规则（不变）

- 叶节点 `H(0x00 || body)`，内部节点 `H(0x01 || left || right)`，空树 root `H(empty)`。
- 非 2 次幂树按“小于叶数的最大 2 次幂”切左右子树，沿用 RFC6962 MTH 语义，
  `root_hash` 语义与原原型完全一致（递归 root oracle 见 `merkle.root_hash`）。
- 叶子 1..128 字节，单 writer 最多 4096 叶。

## 文件

| 文件 | 职责 |
| --- | --- |
| `merkle.py` | 纯协议原语、递归 root oracle、一致性证明生成/验证、membership 证明（无 I/O）。 |
| `logstore.py` | SQLite 持久日志：`leaves` 表 + 持久完美子树索引 `nodes(level,idx,hash)`；`append/append_batch`、`head(size)`、`consistency`、`inclusion`，并记录真实索引读取 `read_trace`。 |
| `witness.py` | **独立**见证：只 `import hashlib,sqlite3`，原语与验证全部自带；只持对数状态 `(size,root)`，验证通过才在单事务内原子推进，失败保留原头。 |
| `proofs.py` | 兼容层，保留旧入口名 `inclusion/verify_inclusion/consistency`。 |
| `selftest.py` | 穷举/随机/独立参考 oracle/逐叶篡改/持久化/同链测试。 |
| `benchmark.py` | 公开基准 `new_size=4096, old_size∈{1,63,1023,4095}` 的读取与证明长度核算。 |
| `probe.py` | 端到端反例与同链演示（替换第二叶、错误旧头、幂次边界、各类畸形证明等）。 |

## 持久子树索引与读取预算

追加叶子时按二进制进位合并，所有产生过的完美子树（瓦片）都写入 `nodes` 表并
不可变，历史检查点因此可直接查询，证明生成**绝不扫描 `leaves` 表**。非完美子树
根由持久瓦片现场折叠，并在单次证明内 memo。

公开基准（`python3 benchmark.py`）实测，四个 old_size 均为：

- 索引节点读取 **13 次**（预算 ≤64，含预读，计数不含最初建索引）；
- 证明 **13 个 hash**（预算 ≤64）；
- `leaves` 表读取 **0 次**。

`read_trace` 逐条记录真实 SQL 索引访问，benchmark 会打印 old=63 的完整读取记录。

## 证明结构（RFC6962 等价）

证明是一串有序哈希。生成（`gen_consistency`）与验证（`verify_consistency`，见证内
另有一份独立实现 `_verify`）严格同构、互为逆过程：验证端从证明同时重算出旧根与
新根，旧根必须等于已信任 `old_root`、新根必须等于新头 `new_root`。任一片旧叶被
替换都会让重算旧根失配而被拒绝。特殊情形：

- 空旧树（`old_size=0`）与同长度同 root：使用空证明；
- 回退、同长度异 root、错绑定 `old_size`、截断、重复/多余节点、非法 hash 一律拒绝。

## 运行

```
python3 selftest.py     # 全部正确性/攻击/持久化测试
python3 benchmark.py    # 4096 叶读取与证明长度基准（含真实读取记录）
python3 probe.py        # 题面要求的反例与同链演示
```
