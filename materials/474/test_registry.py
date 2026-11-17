"""端到端测试：引用锁定、嵌套类型、默认值、传递模式、并发发布与历史解码。

运行： python3 test_registry.py -v
"""
import json
import sqlite3
import threading
import unittest

from catalog import Catalog, canonical_json, digest_of
from compatibility import Incompatible, Registry, can_read
from decode import DecodeError, decode, decode_with_trace, replay_history
from resolver import MAX_REF_DEPTH, Resolver, SchemaError, normalize


NO_DEFAULT = object()


def rec(name, *fields):
    out_fields = []
    for spec in fields:
        if len(spec) == 2:  # (name, schema)：无默认值
            n, s = spec
            out_fields.append({"name": n, "schema": s})
        else:              # (name, schema, default)；NO_DEFAULT 显式表示无默认
            n, s, d = spec
            entry = {"name": n, "schema": s}
            if d is not NO_DEFAULT:
                entry["default"] = d
            out_fields.append(entry)
    return {"type": "record", "name": name, "fields": out_fields}


class RegistryTestBase(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog()
        self.resolver = Resolver(self.catalog)
        self.registry = Registry(self.catalog, self.resolver)


class TestRefPinning(RegistryTestBase):
    def test_ref_resolves_pinned_version_not_latest(self):
        self.registry.register("D", {"type": "int"}, mode="NONE")
        ref = {"type": "ref", "subject": "D", "version": 1}
        self.registry.register("R", rec("R", ("a", ref, NO_DEFAULT)))
        # 依赖发布新版本：旧契约的解释不得改变
        self.registry.register("D", {"type": "string"}, mode="NONE")

        contract = self.registry.get("R", 1)
        self.assertEqual(contract["closure"], [("D", 1)])
        self.assertEqual(contract["body"]["fields"][0]["schema"],
                         {"type": "ref", "subject": "D", "version": 1})

        expanded = self.registry.resolve("R", 1)
        self.assertEqual(expanded["fields"][0]["schema"]["type"], "int")

        # get(D,1) 与 get(D) 返回各自不可变版本，latest 不污染旧契约
        self.assertEqual(self.registry.get("D", 1)["body"], {"type": "int"})
        self.assertEqual(self.registry.get("D", 2)["body"], {"type": "string"})

    def test_missing_ref_fails_whole_registration(self):
        before = self.catalog.all_versions("X")
        with self.assertRaises(SchemaError) as cm:
            self.registry.register("X", rec("X", ("a", {"type": "ref", "subject": "Gone", "version": 3}, NO_DEFAULT)))
        self.assertIn("not published", str(cm.exception))
        self.assertEqual(self.catalog.all_versions("X"), before)  # 无空洞成功回执

    def test_cyclic_ref_rejected(self):
        # 正常发布流程下，每条 ref 都指向更早发布的精确版本，按发布时间归纳不可能成环；
        # 这里用手工构造的闭包（如损坏库）验证两道环检测防线确实生效。
        # 根模式用 array 包裹 ref，使根内不含 record，确保先触发环检测而非重名检测。
        root = {"type": "array",
                "items": {"type": "ref", "subject": "A", "version": 1}}
        a = rec("ARec", ("b", {"type": "ref", "subject": "B", "version": 1}, NO_DEFAULT))
        b = rec("BRec", ("a", {"type": "ref", "subject": "A", "version": 1}, NO_DEFAULT))

        class _StubCatalog:
            def get_contract(self, subject, version):
                return {"subject": subject, "version": version,
                        "body": {("A", 1): a, ("B", 1): b}[(subject, version)],
                        "closure": [], "digest": "stub"}

        with self.assertRaises(SchemaError) as cm:
            Resolver(_StubCatalog()).build_closure(root)
        self.assertIn("cyclic ref", str(cm.exception))

        # expand 路径同样有栈式环检测
        with self.assertRaises(SchemaError) as cm2:
            Resolver(None).expand(root, {("A", 1): a, ("B", 1): b})
        self.assertIn("cyclic", str(cm2.exception))

    def test_self_ref_to_unpublished_version_rejected(self):
        schema = rec("Self", ("me", {"type": "ref", "subject": "Self", "version": 1}, NO_DEFAULT))
        with self.assertRaises(SchemaError):  # v1 尚不存在 => 缺失引用，整次注册失败
            self.registry.register("Self", schema, mode="NONE")

    def test_depth_limit(self):
        # 链 C0 -> C1 -> ... ；深度 8 为上限，第 9 跳必须拒绝
        self.registry.register("C0", {"type": "int"}, mode="NONE")
        for i in range(1, MAX_REF_DEPTH + 1):
            self.registry.register(
                f"C{i}",
                rec(f"C{i}", ("link", {"type": "ref", "subject": f"C{i-1}", "version": 1}, NO_DEFAULT)),
                mode="NONE",
            )
        too_deep = rec("C9", ("link", {"type": "ref", "subject": f"C{MAX_REF_DEPTH}", "version": 1}, NO_DEFAULT))
        with self.assertRaises(SchemaError) as cm:
            self.registry.register("C9", too_deep, mode="NONE")
        self.assertIn("depth", str(cm.exception))

    def test_transitive_closure_is_saved(self):
        self.registry.register("Leaf", {"type": "string"}, mode="NONE")
        self.registry.register("Mid", rec("Mid", ("l", {"type": "ref", "subject": "Leaf", "version": 1}, NO_DEFAULT)),
                               mode="NONE")
        self.registry.register("Top", rec("Top", ("m", {"type": "ref", "subject": "Mid", "version": 1}, NO_DEFAULT)),
                               mode="NONE")
        contract = self.registry.get("Top", 1)
        self.assertEqual(contract["closure"], [("Leaf", 1), ("Mid", 1)])
        # 依赖继续发布，Top v1 的闭包与解释保持不变
        self.registry.register("Leaf", {"type": "int"}, mode="NONE")
        self.assertEqual(self.registry.get("Top", 1)["closure"], [("Leaf", 1), ("Mid", 1)])
        self.assertEqual(self.registry.resolve("Top", 1)["fields"][0]["schema"]
                         ["fields"][0]["schema"]["type"], "string")

    def test_diamond_ref_same_origin_allowed(self):
        self.registry.register("Shared", rec("Shared", ("v", {"type": "int"}, NO_DEFAULT)), mode="NONE")
        self.registry.register("Left", rec("Left", ("s", {"type": "ref", "subject": "Shared", "version": 1}, NO_DEFAULT)),
                               mode="NONE")
        self.registry.register("Right", rec("Right", ("s", {"type": "ref", "subject": "Shared", "version": 1}, NO_DEFAULT)),
                               mode="NONE")
        top = rec("Top",
                  ("l", {"type": "ref", "subject": "Left", "version": 1}, NO_DEFAULT),
                  ("r", {"type": "ref", "subject": "Right", "version": 1}, NO_DEFAULT))
        self.registry.register("Top", top, mode="NONE")  # 同一来源的同名 record 不冲突

    def test_record_name_collision_across_origins_rejected(self):
        self.registry.register("X1", rec("Dup", ("v", {"type": "int"}, NO_DEFAULT)), mode="NONE")
        self.registry.register("X2", rec("Dup", ("v", {"type": "string"}, NO_DEFAULT)), mode="NONE")
        top = rec("Top",
                  ("a", {"type": "ref", "subject": "X1", "version": 1}, NO_DEFAULT),
                  ("b", {"type": "ref", "subject": "X2", "version": 1}, NO_DEFAULT))
        with self.assertRaises(SchemaError) as cm:
            self.registry.register("Top", top, mode="NONE")
        self.assertIn("not unique in closure", str(cm.exception))


class TestCompatibilityRules(RegistryTestBase):
    def test_scalar_promotion_and_rejection(self):
        self.assertTrue(can_read(normalize("long"), normalize("int"))[0])
        self.assertFalse(can_read(normalize("int"), normalize("long"))[0])
        self.assertFalse(can_read(normalize("string"), normalize("int"))[0])
        self.assertTrue(can_read(normalize("string"), normalize("string"))[0])

    def test_null_only_accepts_null(self):
        ok, _ = can_read(normalize("null"), normalize("null"))
        self.assertTrue(ok)
        self.assertFalse(can_read(normalize("null"), normalize("int"))[0])
        self.assertFalse(can_read(normalize("int"), normalize("null"))[0])

    def test_nested_type_change_rejected(self):
        v1 = rec("R", ("a", {"type": "int"}, NO_DEFAULT))
        v2 = rec("R", ("a", {"type": "string"}, NO_DEFAULT))
        self.registry.register("R", v1, mode="NONE")
        with self.assertRaises(Incompatible) as cm:
            self.registry.register("R", v2, mode="BACKWARD_TRANSITIVE")
        fail = cm.exception.failures[0]
        self.assertEqual(fail["reader"], "candidate")
        self.assertEqual(fail["writer"], "v1")
        self.assertIn("$.a", fail["reason"])

    def test_array_recursive_compatibility(self):
        w = {"type": "array", "items": {"type": "int"}}
        r = {"type": "array", "items": {"type": "long"}}
        self.assertTrue(can_read(r, w)[0])
        bad = {"type": "array", "items": {"type": "string"}}
        ok, reason = can_read(bad, w)
        self.assertFalse(ok)
        self.assertIn("$[]", reason)

    def test_reader_added_field_requires_default(self):
        w = rec("R", ("a", {"type": "int"}, NO_DEFAULT))
        no_default = rec("R", ("a", {"type": "int"}, NO_DEFAULT), ("b", {"type": "string"}, NO_DEFAULT))
        with_default = rec("R", ("a", {"type": "int"}, NO_DEFAULT),
                           ("b", {"type": "string"}, "x"))
        self.assertFalse(can_read(no_default, w)[0])
        self.assertTrue(can_read(with_default, w)[0])

    def test_writer_extra_fields_ignored(self):
        r = rec("R", ("a", {"type": "int"}, NO_DEFAULT))
        w = rec("R", ("a", {"type": "int"}, NO_DEFAULT), ("b", {"type": "string"}, NO_DEFAULT))
        self.assertTrue(can_read(r, w)[0])

    def test_record_name_must_match(self):
        ok, reason = can_read(rec("A"), rec("B")) if False else \
            can_read(rec("A", ("x", "int", NO_DEFAULT)), rec("B", ("x", "int", NO_DEFAULT)))
        self.assertFalse(ok)
        self.assertIn("record name mismatch", reason)

    def test_invalid_default_rejected_at_registration(self):
        bad = rec("R", ("a", {"type": "int"}, NO_DEFAULT), ("b", {"type": "int"}, "not-an-int"))
        with self.assertRaises(SchemaError):
            self.registry.register("R", bad, mode="NONE")
        # array 默认值逐项校验
        bad_arr = rec("R", ("tags", {"type": "array", "items": {"type": "int"}}, [1, "x", 3]))
        with self.assertRaises(SchemaError) as cm:
            self.registry.register("R2", bad_arr, mode="NONE")
        self.assertIn("$.tags[1]", str(cm.exception))
        # record 默认值逐字段校验
        bad_rec_default = rec("Outer",
                              ("inner", rec("Inner", ("k", "string", NO_DEFAULT)), {"k": 7}))
        with self.assertRaises(SchemaError):
            self.registry.register("R3", bad_rec_default, mode="NONE")

    def test_duplicate_field_and_unknown_type(self):
        with self.assertRaises(SchemaError):
            normalize(rec("R", ("a", "int", NO_DEFAULT), ("a", "int", NO_DEFAULT)))
        with self.assertRaises(SchemaError):
            normalize({"type": "map", "values": "int"})  # union/map 等不支持
        with self.assertRaises(SchemaError):
            normalize({"type": "union"})

    def test_bool_is_not_valid_int_default_and_ref_default_forbidden(self):
        from resolver import validate_all_defaults
        with self.assertRaises(SchemaError):
            validate_all_defaults(normalize(
                rec("B", ("b", {"type": "int"}, True))))
        with self.assertRaises(SchemaError):
            self.registry.register("Bad", rec("Bad", (
                "d", {"type": "ref", "subject": "X", "version": 1}, 1)), mode="NONE")

    def test_non_transitive_forward_only_compares_latest(self):
        self.registry.register("K", rec("K", ("a", "int", NO_DEFAULT)), mode="NONE")
        self.registry.register("K", rec("K", ("a", "string", NO_DEFAULT)), mode="NONE")
        res = self.registry.register(
            "K", rec("K", ("a", "string", NO_DEFAULT), ("z", "int", 1)), mode="FORWARD")
        self.assertEqual(res["version"], 3)  # 只要求 latest(v2) 能读候选

    def test_nested_record_default_injection(self):
        self.registry.register("Doc", rec("Doc", (
            "meta", rec("Meta", ("k", "string", NO_DEFAULT)), {"k": "v"})), mode="NONE")
        out, trace = decode_with_trace(self.registry.resolve("Doc", 1), {})
        self.assertEqual(out, {"meta": {"k": "v"}})
        self.assertEqual(trace[0]["path"], "$.meta")

    def test_null_default_field(self):
        self.registry.register("N", rec("N", ("x", "null", None)), mode="NONE")
        out, _ = decode_with_trace(self.registry.resolve("N", 1), {})
        self.assertEqual(out, {"x": None})

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            self.registry.register("Q", "int", mode="MAGIC")


class TestTransitiveModes(RegistryTestBase):
    def _build_history(self):
        # v1: int 字段；v2: 提升为 long（向后兼容）；payload 语义沿历史演进
        self.registry.register("S", rec("S", ("n", "int", NO_DEFAULT)), mode="NONE")
        self.registry.register("S", rec("S", ("n", "long", NO_DEFAULT)), mode="BACKWARD_TRANSITIVE")
        self.registry.register("S", rec("S", ("n", "long", NO_DEFAULT), ("tag", "string", "t")),
                               mode="BACKWARD_TRANSITIVE")
        self.assertEqual(self.catalog.all_versions("S"), [1, 2, 3])

    def test_backward_transitive_checks_every_historical_writer(self):
        self._build_history()
        # v4 改字段类型为 string：对 v1/v2/v3 全部不兼容，失败明细必须列全
        with self.assertRaises(Incompatible) as cm:
            self.registry.register("S", rec("S", ("n", "string", NO_DEFAULT), ("tag", "string", "t")),
                                   mode="BACKWARD_TRANSITIVE")
        writers = {f["writer"] for f in cm.exception.failures}
        self.assertEqual(writers, {"v1", "v2", "v3"})  # 中间版本没有被遗漏
        self.assertTrue(all(f["direction"] == "backward" for f in cm.exception.failures))

    def test_forward_transitive_rejects_type_change_old_reader_cannot_read(self):
        self._build_history()  # v1 n:int；v2 n:long；v3 n:long + tag(默认 "t")

        # 新 writer 把共享字段 n 改成 string：v1(int)/v2(long)/v3(long) 三个历史 reader
        # 都读不了 string => FORWARD_TRANSITIVE 必须失败，且失败明细覆盖每个历史版本
        breaking = rec("S", ("n", "string", NO_DEFAULT), ("tag", "string", "t"))
        with self.assertRaises(Incompatible) as cm:
            self.registry.register("S", breaking, mode="FORWARD_TRANSITIVE")
        self.assertTrue(all(f["direction"] == "forward" for f in cm.exception.failures))
        self.assertEqual({f["reader"] for f in cm.exception.failures}, {"v1", "v2", "v3"})

        # 一个能通过的 forward 演进：共享字段保持 int，仅新增带默认值的 big 字段
        # （旧 reader 不认识新字段 -> 多余字段忽略；类型与所有历史 reader 一致）
        ok_schema = rec("S", ("n", "int", NO_DEFAULT), ("tag", "string", "t"),
                        ("big", "long", 0))
        self.assertEqual(
            self.registry.register("S", ok_schema, mode="FORWARD_TRANSITIVE")["version"], 4)
        # 库中没有留下失败的 breaking 版本，历史仍是 1..3 + 新的 4
        self.assertEqual(self.catalog.all_versions("S"), [1, 2, 3, 4])

    def test_forward_transitive_field_removal_is_allowed_but_ignored_in_decode(self):
        # 独立 subject，共享字段始终为 int，避免历史 int->long 提升干扰 forward 判断
        self.registry.register("FR", rec("FR", ("n", "int", NO_DEFAULT)), mode="NONE")
        self.registry.register(
            "FR", rec("FR", ("n", "int", NO_DEFAULT), ("tag", "string", "t")),
            mode="FORWARD_TRANSITIVE")
        self.registry.register(
            "FR", rec("FR", ("n", "int", NO_DEFAULT), ("tag", "string", "t"),
                      ("note", "string", "")),
            mode="FORWARD_TRANSITIVE")
        # v4 删除 tag：旧 reader(v3) 不认识的变化只是数据里少了字段——
        # 读取时 tag 走 reader 自己的默认值注入，因此 forward 兼容、发布通过
        res = self.registry.register(
            "FR", rec("FR", ("n", "int", NO_DEFAULT), ("note", "string", "")),
            mode="FORWARD_TRANSITIVE")
        self.assertEqual(res["version"], 4)
        old_reader = self.registry.resolve("FR", 3)
        out, trace = decode_with_trace(old_reader, {"n": 5, "note": ""})
        self.assertEqual(out, {"n": 5, "note": "", "tag": "t"})
        self.assertIn({"event": "default_injected", "path": "$.tag", "value": "t"}, trace)

    def test_full_transitive_requires_both_directions(self):
        # 独立 subject：新增带默认字段双向都成立（多余字段可忽略）
        self.registry.register("F", rec("F", ("n", "int", NO_DEFAULT)), mode="NONE")
        ok_schema = rec("F", ("n", "int", NO_DEFAULT), ("tag", "string", "t"))
        self.assertEqual(
            self.registry.register("F", ok_schema, mode="FULL_TRANSITIVE")["version"], 2)

        # int -> long：backward 成立（long reader 能读 int writer），
        # 但 forward 不成立（v1/v2 的 int reader 读不了 long writer）=> FULL 必须拒绝
        promotion = rec("F", ("n", "long", NO_DEFAULT), ("tag", "string", "t"))
        with self.assertRaises(Incompatible) as cm:
            self.registry.register("F", promotion, mode="FULL_TRANSITIVE")
        directions = {f["direction"] for f in cm.exception.failures}
        self.assertEqual(directions, {"forward"})
        # 同一演进在 BACKWARD_TRANSITIVE 下通过
        self.assertEqual(
            self.registry.register("F", promotion, mode="BACKWARD_TRANSITIVE")["version"], 3)

    def test_non_transitive_only_compares_latest(self):
        # v1 int -> v2 string（NONE 放行）；v3 string+默认字段对 latest(v2) 向后兼容即可，
        # 即使它读不了 v1 —— 非传递模式不看 v1
        self.registry.register("T", rec("T", ("n", "int", NO_DEFAULT)), mode="NONE")
        self.registry.register("T", rec("T", ("n", "string", NO_DEFAULT)), mode="NONE")
        res = self.registry.register(
            "T", rec("T", ("n", "string", NO_DEFAULT), ("note", "string", "")), mode="BACKWARD")
        self.assertEqual(res["version"], 3)
        # 同一演进在 TRANSITIVE 下必须失败（v1 是 int writer）
        with self.assertRaises(Incompatible) as cm:
            self.registry.register(
                "T", rec("T", ("n", "string", NO_DEFAULT), ("note2", "string", "")),
                mode="BACKWARD_TRANSITIVE")
        self.assertIn("v1", {f["writer"] for f in cm.exception.failures})


class TestIdempotencyAndConcurrency(RegistryTestBase):
    def test_retry_returns_same_version(self):
        schema = rec("R", ("a", "int", NO_DEFAULT))
        first = self.registry.register("R", schema, mode="BACKWARD_TRANSITIVE")
        second = self.registry.register("R", {"type": "record", "name": "R", "fields": [
            {"name": "a", "schema": {"type": "int"}}]}, mode="BACKWARD_TRANSITIVE")
        self.assertEqual(first["version"], 1)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["version"], 1)
        self.assertEqual(first["digest"], second["digest"])
        self.assertEqual(self.catalog.all_versions("R"), [1])

    def test_same_body_different_closure_is_new_version(self):
        self.registry.register("D", "int", mode="NONE")
        self.registry.register("D", "long", mode="NONE")
        r1 = self.registry.register("R", rec("R", ("x", {"type": "ref", "subject": "D", "version": 1}, NO_DEFAULT)),
                                    mode="NONE")
        r2 = self.registry.register("R", rec("R", ("x", {"type": "ref", "subject": "D", "version": 2}, NO_DEFAULT)),
                                    mode="NONE")
        self.assertNotEqual(r1["digest"], r2["digest"])
        self.assertEqual((r1["version"], r2["version"]), (1, 2))

    def test_concurrent_registrations_serialize_without_stale_history(self):
        # FORWARD 模式下“新 writer 新增无默认字段”对 v1 reader 兼容（reader 忽略多余字段）。
        # 两个并发注册各加不同字段：先提交者基于历史 v1 通过；后提交者若也基于同一份过时历史
        # 就会双写。正确行为：它必须看到刚发布的 v2，并因 v2 reader 不认识自己的新字段
        # （该字段在 v2 中没有默认值）而被拒绝。
        self.registry.register("C", rec("C", ("a", "int", NO_DEFAULT)), mode="NONE")
        barrier = threading.Barrier(2)
        results = {}

        def worker(tag, field_name):
            barrier.wait()
            try:
                res = self.registry.register(
                    "C", rec("C", ("a", "int", NO_DEFAULT), (field_name, "string", NO_DEFAULT)),
                    mode="FORWARD_TRANSITIVE")
                results[tag] = ("ok", res["version"])
            except Incompatible as exc:
                results[tag] = ("rejected", exc.failures[0]["reason"])
            except sqlite3.OperationalError as exc:  # 不应发生：写锁与忙等已在内部处理
                results[tag] = ("db-error", str(exc))

        t1 = threading.Thread(target=worker, args=("t1", "b"))
        t2 = threading.Thread(target=worker, args=("t2", "c"))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(sorted(r[0] for r in results.values()), ["ok", "rejected"])
        winner = next(tag for tag, r in results.items() if r[0] == "ok")
        self.assertEqual(results[winner][1], 2)
        self.assertEqual(self.catalog.all_versions("C"), [1, 2])  # 没有双写

    def test_concurrent_identical_registrations_dedup(self):
        barrier = threading.Barrier(3)
        results = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            try:
                res = self.registry.register("D", rec("D", ("a", "int", NO_DEFAULT)), mode="NONE")
                with lock:
                    results.append(res["version"])
            except Exception as exc:  # 任何异常都让断言失败时可见，而不是静默丢结果
                with lock:
                    results.append(f"ERROR:{exc}")

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [1, 1, 1])  # 串行化 + 唯一索引兜底，全部拿到原版本
        self.assertEqual(self.catalog.all_versions("D"), [1])

    def test_limits(self):
        for i in range(10):
            self.registry.register(f"S{i}", "int", mode="NONE")
        with self.assertRaises(SchemaError):
            self.registry.register("S10", "int", mode="NONE")
        # 每个版本正文必须不同，否则命中幂等去重；用嵌套 int 字段宽度递增构造 20 个版本
        schema = "int"
        for v in range(2, 21):
            schema = rec(f"L{v}", ("inner", schema, NO_DEFAULT))
            self.registry.register("S0", schema, mode="NONE")
        self.assertEqual(len(self.catalog.all_versions("S0")), 20)
        with self.assertRaises(SchemaError):
            self.registry.register("S0", rec("L21", ("inner", schema, NO_DEFAULT)), mode="NONE")


class TestDecodeAndHistory(RegistryTestBase):
    def test_default_injection_vs_explicit_null(self):
        schema = rec("R",
                     ("a", "int", NO_DEFAULT),
                     ("b", "string", "dflt"))
        expanded = self.resolver.expand(schema, {})
        result, trace = decode_with_trace(expanded, {"a": 1})
        self.assertEqual(result, {"a": 1, "b": "dflt"})
        self.assertEqual(trace, [{"event": "default_injected", "path": "$.b", "value": "dflt"}])

        # 显式 null 不等于缺失：string 类型拒绝 null
        with self.assertRaises(DecodeError) as cm:
            decode(expanded, {"a": 1, "b": None})
        self.assertIn("$.b", str(cm.exception))

    def test_null_field_accepts_explicit_null(self):
        schema = rec("R", ("a", "null", NO_DEFAULT))
        self.assertEqual(decode(schema, {"a": None}), {"a": None})
        with self.assertRaises(DecodeError):
            decode(schema, {"a": 1})

    def test_array_item_wise_validation_and_ignored_fields(self):
        schema = rec("R", ("tags", {"type": "array", "items": "string"}, NO_DEFAULT))
        out, trace = decode_with_trace(schema, {"tags": ["x", "y"], "ghost": 1})
        self.assertEqual(out, {"tags": ["x", "y"]})
        self.assertIn({"event": "field_ignored", "path": "$.ghost"}, trace)
        with self.assertRaises(DecodeError) as cm:
            decode(schema, {"tags": ["ok", 3]})
        self.assertIn("$.tags[1]", str(cm.exception))

    def test_int_to_long_promotion_on_real_history(self):
        self.registry.register("N", rec("N", ("n", "int", NO_DEFAULT)), mode="NONE")
        self.registry.register("N", rec("N", ("n", "long", NO_DEFAULT)), mode="BACKWARD_TRANSITIVE")
        writer_payload = {"n": 42}  # 按 v1(int) 写出的真实历史记录
        reader = self.registry.resolve("N", 2)
        result, trace = decode_with_trace(reader, writer_payload)
        self.assertEqual(result, {"n": 42})  # v2 reader 成功读 v1 数据
        self.assertIn({"event": "int_promoted_to_long", "path": "$.n", "value": 42}, trace)
        # long 值域（超出 int32）的值不产生提升事件
        big = 2**40
        self.assertEqual(decode_with_trace(reader, {"n": big})[0], {"n": big})

    def test_replay_history_proves_compatibility_with_decoded_values(self):
        self.registry.register("Evt", rec("Evt", ("id", "int", NO_DEFAULT)), mode="NONE")
        self.registry.register("Evt", rec("Evt", ("id", "int", NO_DEFAULT), ("kind", "string", "std")),
                               mode="BACKWARD_TRANSITIVE")
        self.registry.register("Evt", rec("Evt", ("id", "long", NO_DEFAULT), ("kind", "string", "std")),
                               mode="BACKWARD_TRANSITIVE")
        payload = {"id": 7}  # 按 v1(int writer) 写出的真实历史记录
        report = replay_history(self.registry, "Evt", writer_version=1, payload=payload)
        by_reader = {e["reader"]: e for e in report}

        # v3 reader 读 v1 writer：int->long 提升 + kind 缺失注入默认值
        v3 = by_reader["v3"]
        self.assertTrue(v3["compatible"])
        self.assertEqual(v3["decoded"], {"id": 7, "kind": "std"})
        self.assertIn({"event": "default_injected", "path": "$.kind", "value": "std"},
                      v3["trace"])

        # v1 reader 读同一条 v1 数据：无注入无忽略，原样返回
        v1 = by_reader["v1"]
        self.assertEqual(v1["decoded"], {"id": 7})
        self.assertEqual(v1["trace"], [])

    def test_replay_history_flags_incompatible_reader_with_reason(self):
        # 用 NONE 强制制造一个 v2(string)，让它作为 reader 去读 v1(int) 真实记录
        self.registry.register("Raw", rec("Raw", ("a", "int", NO_DEFAULT)), mode="NONE")
        self.registry.register("Raw", rec("Raw", ("a", "string", NO_DEFAULT)), mode="NONE")
        report = replay_history(self.registry, "Raw", writer_version=1, payload={"a": 1})
        v2 = next(e for e in report if e["reader"] == "v2")
        self.assertFalse(v2["compatible"])
        self.assertEqual(v2["writer"], "v1")
        self.assertIn("scalar mismatch", v2["reason"])

    def test_incompatible_decode_path_reports_versions_and_reason(self):
        self.registry.register("P", rec("P", ("a", "int", NO_DEFAULT)), mode="NONE")
        try:
            self.registry.register("P", rec("P", ("a", "string", NO_DEFAULT)), mode="BACKWARD_TRANSITIVE")
            self.fail("expected Incompatible")
        except Incompatible as exc:
            detail = exc.failures[0]
            self.assertEqual(detail["reader"], "candidate")
            self.assertEqual(detail["writer"], "v1")
            self.assertIn("scalar mismatch", detail["reason"])


class TestPersistenceImmutability(unittest.TestCase):
    def test_contract_roundtrip_on_disk(self):
        import os
        import tempfile

        path = tempfile.mktemp(suffix=".db")
        try:
            cat = Catalog(path)
            reg = Registry(cat, Resolver(cat))
            reg.register("D", "int", mode="NONE")
            reg.register("R", rec("R", ("d", {"type": "ref", "subject": "D", "version": 1}, NO_DEFAULT)),
                         mode="NONE")
            cat.close()

            cat2 = Catalog(path)
            reg2 = Registry(cat2, Resolver(cat2))
            contract = reg2.get("R", 1)
            self.assertEqual(contract["closure"], [("D", 1)])
            self.assertEqual(reg2.resolve("R", 1)["fields"][0]["schema"]["type"], "int")
            cat2.close()
        finally:
            for p in (path, path + "-wal", path + "-shm"):
                if os.path.exists(p):
                    os.remove(p)

    def test_digest_stability(self):
        body = rec("R", ("a", "int", NO_DEFAULT))
        closure = [("D", 2), ("D", 1)]
        d1 = digest_of(body, closure)
        d2 = digest_of(json.loads(canonical_json(body)), [("D", 1), ("D", 2)])
        self.assertEqual(d1, d2)  # 闭包顺序不影响摘要


if __name__ == "__main__":
    unittest.main(verbosity=2)
