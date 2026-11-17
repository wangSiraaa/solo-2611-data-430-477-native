"""原始缺陷回归探针：

旧实现的两个问题：
  1) Resolver 忽略 ref.version 总取 latest —— 依赖发布新版本会改变旧契约的解释
  2) 兼容性只比上一个版本的字段名 —— 嵌套类型改变也会被接受

现在两个断言都必须成立：
  - 锁定 v1(int) 的 ref 在 D 发布 v2(string) 后仍解析为 int
  - 嵌套 int->string 的演进被 BACKWARD_TRANSITIVE 拒绝，且失败明细带版本与原因
"""
import json

from catalog import Catalog
from compatibility import Incompatible, Registry
from resolver import Resolver

c = Catalog()
r = Resolver(c)
g = Registry(c, r)

# --- 缺陷 1：引用必须锁定精确版本 -----------------------------------
g.register("D", {"type": "int"}, mode="NONE")
ref = {"type": "ref", "subject": "D", "version": 1}
g.register("R", {"type": "record", "name": "R",
                 "fields": [{"name": "a", "schema": ref}]})
g.register("D", {"type": "string"}, mode="NONE")  # 依赖后来发布了不兼容的新版本

contract = g.get("R", 1)
resolved = g.resolve("R", 1)["fields"][0]["schema"]["type"]
assert resolved == "int", resolved
assert contract["closure"] == [("D", 1)]
assert contract["body"]["fields"][0]["schema"]["version"] == 1

# --- 缺陷 2：嵌套类型改变必须被传递兼容性拒绝 -----------------------
v1 = {"type": "record", "name": "R2",
      "fields": [{"name": "a", "schema": {"type": "int"}}]}
g.register("R2", v1)
try:
    g.register("R2", {"type": "record", "name": "R2",
                      "fields": [{"name": "a", "schema": {"type": "string"}}]})
    raise SystemExit("nested incompatible change was wrongly accepted")
except Incompatible as exc:
    detail = exc.failures[0]
    assert detail["reader"] == "candidate" and detail["writer"] == "v1"
    assert "$.a" in detail["reason"]

print(json.dumps({
    "pinned_ref_after_new_version": resolved,
    "expected": "int",
    "closure_saved": contract["closure"],
    "nested_incompatible_accepted": False,
    "rejection": {"reader": detail["reader"], "writer": detail["writer"],
                  "reason": detail["reason"]},
}, ensure_ascii=False))
