# 数据契约注册表：固定引用闭包 + 传递兼容性

迷你 Avro 式类型系统（非通用 Avro 实现）：仅支持
`int / long / string / null / array(items) / record(name,fields) / ref(subject,version)`，
不实现 union、alias、logical type 与 Avro 二进制编码。

## 文件

| 文件 | 职责 |
| --- | --- |
| `catalog.py` | SQLite 持久化与事务边界。每个版本保存规范正文 `body`、引用解析闭包 `closure`、摘要 `digest`；`(subject, digest)` 唯一索引兜底幂等。 |
| `resolver.py` | 模式规范化、默认值逐项校验、引用闭包构建（精确版本/循环/深度 8/record 闭包内重名）、按锁定闭包展开。 |
| `compatibility.py` | 递归 `can_read(reader, writer)`、七种兼容模式、单事务注册流程（校验→闭包→兼容→幂等→版本分配→发布）。 |
| `decode.py` | 小型 JSON 式 decode：缺失字段默认值注入、显式 null 区分、array 逐项校验、历史记录回放 `replay_history`。 |
| `probe.py` | 题面两个原始缺陷的回归探针。 |
| `demo.py` | 端到端演示（引用锁定/嵌套变更/默认值/传递模式/幂等/并发/历史解码）。 |
| `test_registry.py` | 36 个 unittest 用例。 |

## 关键语义

- **引用锁定**：`ref` 必须指向已发布的精确版本；发布时保存完整解析闭包，
  `get(subject, version)` 永远返回同一可解析契约，依赖后来发布的 latest 不会嵌入旧契约。
  缺失引用、循环、闭包深度 > 8、record 名在闭包内不唯一，均整次注册失败且无写入。
- **兼容规则**：同标量可读；唯一提升是 writer `int` → reader `long`；`null` 只被 `null`
  接纳；array 递归；record 同 name 按字段名匹配，reader 新增字段必须有合法类型默认值
  （array 默认值逐项校验），writer 多余字段忽略，同名字段递归可读。
- **传递模式**：`*_TRANSITIVE` 与该 subject 的**每一个**历史版本比较（不跳过中间版本）；
  非传递模式只比最新版本。失败时 `Incompatible.failures` 给出每条路径的
  reader/writer 版本与原因。
- **事务与并发**：闭包解析、历史读取、兼容检查、版本分配、发布在同一个
  `BEGIN IMMEDIATE` 写事务内（进程内另有互斥锁），两个并发注册不可能同时基于过时历史通过。
  同规范正文 + 同闭包的重试返回原版本（兼容检查先于幂等返回，不发空洞成功回执）。
- **容量**：最多 10 个 subject、每个 subject 20 个版本、引用闭包深度 8。

## 运行

```bash
python3 test_registry.py   # 36 个用例
python3 probe.py           # 原始缺陷回归
python3 demo.py            # 端到端行为演示
```
