# Question 441

本题现有源码及输入位于 `materials/441/`。题面中的裸文件名和示例命令以该目录为工作目录；请先在该目录阅读和运行现有入口。保留题面指定的材料相对路径。

## 度量查询引擎

有限 typed JSON AST 引擎，语义合同见 `semantics.md`，三个可复用入口：

- `Model(document)`：加载并校验模型（声明、类型、主键、外键、关系环、度量引用环、
  AST 深度/节点预算），所有拒绝抛出 `ModelError`（`ValueError` 子类），失败不产生
  可用模型，也不影响其他共享模型。
- `Model.visible(table, context)`：公开的受限域接口。表在有显式列约束或受上游
  传播约束时为受限；受限父域沿当前 active 单向关系过滤子外键（NULL 不匹配），
  未受限父关系不剔除 NULL 外键行；多条传入约束自然取交。
- `Evaluator(model).evaluate(node, context)` 与 `QueryEngine(model).run(query)`：
  表达式与分组查询入口，返回结果保留 `columns` 与 `rows`。

关键语义：

- `Context` 持有 `filters` / `row_stack` / 调用级 `relationships`，每个请求与每个
  分组组合都复制独立上下文，共享 `Model` 不保存任何请求状态，关系覆盖仅在求值调用内有效。
- 裸 `sum` / `countrows` 与 `sumx` 只读筛选、不做行到筛选转换；命名 `measure`
  引用和 `calculate` 先做 context transition（外到内的 row_stack 生成单值筛选，
  同表内层行覆盖外层行，其余表筛选保留）。
- `calculate` 严格按 modifiers 从左到右执行 `set` / `keep` / `remove` 与
  `relationship` 局部覆盖；嵌套返回后恢复上层筛选与关系。

## 运行

```bash
python3 run.py model.json normal.json   # 普通分组：east/west 收入 10/30
python3 probe.py                        # 固定 SQL 对照探针（94/40、30/47、visible）
python3 test_semantics.py               # 独立小表 + SQLite 对照的合同测试
```
