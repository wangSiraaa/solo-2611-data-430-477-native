"""端到端演示：用真实发布历史与真实记录证明每一项结论。

运行： python3 demo.py
覆盖：引用锁定 / 嵌套类型改变 / 默认值注入与显式 null / 传递模式（含失败明细）/
      幂等重试 / 并发发布串行化 / 历史记录解码回放。
"""
import json
import sqlite3
import threading

from catalog import Catalog
from compatibility import Incompatible, Registry
from decode import decode_with_trace, replay_history
from resolver import Resolver, SchemaError


def line(title):
    print(f"\n=== {title} ===")


def main():
    catalog = Catalog()
    registry = Registry(catalog, Resolver(catalog))

    # 1) 引用锁定：Order 依赖 Money v1(int 分值)；Money v2 改成 string 后旧契约解释不变
    line("1. 引用闭包锁定精确版本，latest 不回灌旧契约")
    registry.register("Money", {"type": "int"}, mode="NONE")
    order_v1 = {"type": "record", "name": "Order", "fields": [
        {"name": "id", "schema": {"type": "long"}},
        {"name": "amount", "schema": {"type": "ref", "subject": "Money", "version": 1}},
    ]}
    registry.register("Order", order_v1)
    registry.register("Money", {"type": "string"}, mode="NONE")  # 后来的不兼容发布
    contract = registry.get("Order", 1)
    expanded = registry.resolve("Order", 1)
    print("契约闭包:", contract["closure"])
    print("Order v1 中 amount 的解释:", expanded["fields"][1]["schema"]["type"],
          "（即使 Money latest 已是 string）")

    # 2) 嵌套类型改变：BACKWARD_TRANSITIVE 拒绝并给出 reader/writer 版本与原因
    line("2. 嵌套类型改变被传递兼容性拒绝")
    registry.register("Item", {"type": "record", "name": "Item", "fields": [
        {"name": "qty", "schema": {"type": "int"}}]}, mode="NONE")
    try:
        registry.register("Item", {"type": "record", "name": "Item", "fields": [
            {"name": "qty", "schema": {"type": "string"}}]})
    except Incompatible as exc:
        for f in exc.failures:
            print(f"  不兼容: reader={f['reader']} writer={f['writer']} 原因={f['reason']}")

    # 3) 默认值：带合法默认值的新增字段可以演进；显式 null 与缺失不同
    line("3. 默认值注入 vs 显式 null")
    registry.register("User", {"type": "record", "name": "User", "fields": [
        {"name": "id", "schema": {"type": "int"}},
        {"name": "tier", "schema": {"type": "string"}, "default": "free"},
        {"name": "tags", "schema": {"type": "array", "items": {"type": "string"}},
         "default": []},
    ]})
    user_reader = registry.resolve("User", 1)
    decoded, trace = decode_with_trace(user_reader, {"id": 9})
    print("缺失字段解码结果:", decoded)
    print("注入轨迹:", trace)
    try:
        decode_with_trace(user_reader, {"id": 9, "tier": None})
    except Exception as exc:
        print("显式 null 被拒:", exc)

    # 4) 传递模式：三个历史版本，演进必须对每个 writer 成立，中间版本不遗漏
    line("4. BACKWARD_TRANSITIVE 覆盖全部历史 writer")
    registry.register("Evt", {"type": "record", "name": "Evt", "fields": [
        {"name": "id", "schema": {"type": "int"}}]}, mode="NONE")
    registry.register("Evt", {"type": "record", "name": "Evt", "fields": [
        {"name": "id", "schema": {"type": "int"}},
        {"name": "kind", "schema": {"type": "string"}, "default": "std"}]})
    registry.register("Evt", {"type": "record", "name": "Evt", "fields": [
        {"name": "id", "schema": {"type": "long"}},
        {"name": "kind", "schema": {"type": "string"}, "default": "std"}]})
    try:
        registry.register("Evt", {"type": "record", "name": "Evt", "fields": [
            {"name": "id", "schema": {"type": "string"}},
            {"name": "kind", "schema": {"type": "string"}, "default": "std"}]})
    except Incompatible as exc:
        print("被拒的历史 writer 集合:", sorted({f["writer"] for f in exc.failures}))

    # 5) 真实历史记录回放：v1 写出的记录被每个已发布 reader 实际解码
    line("5. 用真实历史记录回放，而不是只比较布尔值")
    report = replay_history(registry, "Evt", writer_version=1, payload={"id": 7})
    for entry in report:
        line_detail = {k: v for k, v in entry.items() if k in
                       ("reader", "writer", "compatible", "reason", "decoded")}
        print(" ", json.dumps(line_detail, ensure_ascii=False))

    # 6) 幂等重试：同正文同闭包返回原版本
    line("6. 同规范同闭包重试不新增版本")
    again = registry.register("User", {"type": "record", "name": "User", "fields": [
        {"name": "id", "schema": {"type": "int"}},
        {"name": "tier", "schema": {"type": "string"}, "default": "free"},
        {"name": "tags", "schema": {"type": "array", "items": {"type": "string"}},
         "default": []},
    ]})
    print("重试返回:", again, "| 历史版本:", catalog.all_versions("User"))

    # 7) 并发发布：两个不同演进同时注册，恰好一个生效，另一个基于最新历史被拒
    line("7. 并发发布的事务串行化")
    registry.register("Counter", {"type": "record", "name": "Counter", "fields": [
        {"name": "n", "schema": {"type": "int"}}]}, mode="NONE")
    barrier = threading.Barrier(2)
    outcomes = {}

    def publish(tag, extra_field):
        barrier.wait()
        try:
            res = registry.register("Counter", {"type": "record", "name": "Counter", "fields": [
                {"name": "n", "schema": {"type": "int"}},
                {"name": extra_field, "schema": {"type": "string"}}]},
                mode="FORWARD_TRANSITIVE")
            outcomes[tag] = ("published", res["version"])
        except Incompatible as exc:
            outcomes[tag] = ("rejected", exc.failures[0]["reason"])
        except sqlite3.OperationalError as exc:
            outcomes[tag] = ("db-error", str(exc))

    threads = [threading.Thread(target=publish, args=("t1", "a")),
               threading.Thread(target=publish, args=("t2", "b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print("并发结果:", outcomes)
    print("Counter 实际版本:", catalog.all_versions("Counter"))

    # 8) 缺失引用整次注册失败，不留空洞回执
    line("8. 缺失/非法引用整次注册失败")
    try:
        registry.register("Bad", {"type": "record", "name": "Bad", "fields": [
            {"name": "x", "schema": {"type": "ref", "subject": "Missing", "version": 9}}]})
    except SchemaError as exc:
        print("拒绝原因:", exc)
    print("Bad 是否存在:", catalog.subject_exists("Bad"))


if __name__ == "__main__":
    main()
