"""探针：用真实 SQLite 虚表调用（BestIndexObject/Filter）核对结果与游标协议。

用法::

    python3 probe.py normal | residual | order | reuse
    python3 probe.py <其它场景>
    python3 probe.py all            # 小数据集跑全部场景
    python3 probe.py big <场景>     # 把源换成 3000 行合法新数据后再跑

每个场景都用同一批数据在独立的**普通 SQLite 表**上做结果对照，
同时检查远端分页/关闭日志与游标泄漏。回调全部走 APSW+SQLite 内核，
不存在脱离内核的模拟。
"""

import json
import sys

import apsw

from remote import Remote
from virtual import connect
import planner as planner_mod

# 含 NULL、重名、会让“文本金额排序”与“整数金额排序”分叉的数据（'10' < '2'）。
SMALL_ROWS = [
    (1, "ann", 2),
    (2, "anne", 10),
    (3, "bob", 3),
    (4, "ann", None),
    (5, "Ann", 20),
    (6, "anna", 100),
    (7, None, 25),
]

PAGE_SIZE = 2


# ---------------------------------------------------------------------- #
# 环境搭建
# ---------------------------------------------------------------------- #
def make_remote(rows):
    return Remote(rows)


def make_oracle(rows):
    """独立普通 SQLite 表，作为结果对照基准。"""
    db = apsw.Connection(":memory:")
    db.execute("create table facts(id INTEGER, name TEXT, amount INTEGER)")
    db.executemany("insert into facts values(?,?,?)", rows)
    return db


def make_big_rows(n=3000):
    """合法新数据：id 唯一、各列允许 NULL、文本/整数排序可分叉。"""
    rows = []
    names = ["ann", "anne", "Anna", "bob", "BOB", "carol", None]
    for i in range(1, n + 1):
        name = names[i % len(names)]
        amount = None if i % 13 == 0 else (i * 7) % 101
        rows.append((i, name, amount))
    return rows


def snapshot_plans():
    """记录真实 BestIndexObject 的协商决策（omit/argv/orderByConsumed）。

    IndexInfo 的取值器只在 BestIndex 调用上下文中有效，因此快照必须在
    包装函数内、调用期间完成。
    """
    seen = []
    original = planner_mod.choose

    def spy(info):
        before = {
            "constraints": [
                {
                    "column": info.get_aConstraint_iColumn(i),
                    "op": info.get_aConstraint_op(i),
                    "usable": info.get_aConstraint_usable(i),
                    "in": info.get_aConstraintUsage_in(i),
                    "collation": info.get_aConstraint_collation(i),
                }
                for i in range(info.nConstraint)
            ],
            "orderBy": [
                {
                    "column": info.get_aOrderBy_iColumn(i),
                    "desc": info.get_aOrderBy_desc(i),
                }
                for i in range(info.nOrderBy)
            ],
        }
        result = original(info)
        after = {
            "usage": [
                {
                    "argvIndex": info.get_aConstraintUsage_argvIndex(i),
                    "omit": info.get_aConstraintUsage_omit(i),
                    "in": info.get_aConstraintUsage_in(i),
                }
                for i in range(info.nConstraint)
            ],
            "orderByConsumed": info.orderByConsumed,
            "idxStr": info.idxStr,
        }
        seen.append({"request": before, "decision": after})
        return result

    planner_mod.choose = spy
    return seen, lambda: planner_mod.__dict__.__setitem__("choose", original)


# ---------------------------------------------------------------------- #
# 场景定义：返回 (sql, bindings 列表或 None, 附加检查)
# ---------------------------------------------------------------------- #
def scenarios():
    return {
        # 1) id 等值：公开的窄访问，实际只应读到命中行，且可重复 prepared 绑定。
        "normal": (
            "select id,name,amount from facts where id=?",
            [[1], [3], [1], [99]],
            {"must_omit": True, "max_rows_per_cursor": 1},
        ),
        # 2) 名称等值：源只给 casefold 前缀候选，残余等值由内核过滤。
        "residual": (
            "select id,name,amount from facts where name=? order by id",
            [["ann"], ["ANNE"], ["bob"], ["nobody"]],
            {"must_omit": False, "prefix_candidate": True},
        ),
        # 3) amount INTEGER 排序：源的文本排序不能被宣称消费，NULL 也由内核排。
        "order": (
            "select id,amount from facts where amount is not null order by amount",
            None,
            {"must_order_consumed": False},
        ),
        # 4) join 重复 Filter：同一虚表游标被反复以不同绑定重新 Filter。
        "reuse": (
            "select o.id,f.id from (select 1 id union all select 3 union all "
            "select 1) o join facts f on f.id=o.id order by o.id, f.id",
            None,
            {"must_omit": True, "no_leak": True},
        ),
        # 5) outer join：内核决定约束是否 usable；结果须含 NULL 扩展且无泄漏。
        #    对 unusable 标志的严格处理另见 planner_units 场景（直接造 IndexInfo）。
        "outer": (
            "select o.id,f.id from (select 7 id union all select 1) o "
            "left join facts f on f.id=o.id order by o.id",
            None,
            {"no_leak": True},
        ),
        # 6) NULL 谓词：IS NULL 无法下推，内核残余；结果须与普通表一致。
        "nulls": (
            "select id,name,amount from facts where amount is null order by id",
            None,
            {"must_omit": False},
        ),
        # 7) 数值范围 + 排序：范围不可下推，INTEGER 排序仍由内核完成。
        "range": (
            "select id,amount from facts where amount between 2 and 10 "
            "order by amount desc, id",
            None,
            {"must_order_consumed": False},
        ),
        # 8) LIKE：源不支持，完全留作残余，且结果不得漏行。
        "like": (
            "select id,name from facts where name like 'an%' order by id",
            None,
            {"must_omit": False},
        ),
        # 9) 不同 collation：NOCASE 等值只能取前缀候选，残余仍归内核。
        "collation": (
            "select id,name from facts where name='ANN' collate nocase "
            "order by id",
            None,
            {"must_omit": False, "prefix_candidate": True},
        ),
        # 10) 提前 LIMIT：有残余/排序时 LIMIT 不得提前作用于候选源。
        "limit": (
            "select id,amount from facts order by amount limit 3",
            None,
            {"must_order_consumed": False, "limit_not_pushed": True},
        ),
        # 11) 两个虚表实例同时打开：页位置不得共享。
        "selfjoin": (
            "select a.id,b.id from facts a join facts b on a.name=b.name "
            "where a.id=1 and b.id in (1,4) order by b.id",
            None,
            {"no_leak": True},
        ),
        # 12) IN 列表：源不支持多值 rhs，内核展开/残余，结果对照普通表。
        "inlist": (
            "select id from facts where id in (2,4,6) order by id",
            None,
            {"must_omit": False},
        ),
    }


# ---------------------------------------------------------------------- #
# 执行与核对
# ---------------------------------------------------------------------- #
def run_case(remote, oracle, sql, bindings):
    db = connect(remote)
    try:
        if bindings is None:
            got = [list(r) for r in db.execute(sql)]
            expected = [list(r) for r in oracle.execute(sql)]
        else:
            # 同一个 prepared 语句反复绑定执行，验证不沿用旧绑定值。
            stmt = db.cursor()
            ostmt = oracle.cursor()
            got, expected = [], []
            for params in bindings:
                got += [list(r) for r in stmt.execute(sql, params)]
                expected += [list(r) for r in ostmt.execute(sql, params)]
        return got, expected
    finally:
        db.close()


def check_invariants(result, plans, remote, spec, got, expected):
    notes = []
    decisions = [p["decision"] for p in plans]
    requests = [p["request"] for p in plans]

    if spec.get("must_omit") is not None:
        omit_values = {u["omit"] for d in decisions for u in d["usage"]}
        if spec["must_omit"]:
            ok = any(u["omit"] for d in decisions for u in d["usage"])
        else:
            ok = not any(u["omit"] for d in decisions for u in d["usage"])
        notes.append({"omit_as_expected": ok, "omit_seen": sorted(omit_values)})
        result["ok"] = result.get("ok", True) and ok

    if "must_order_consumed" in spec:
        consumed = any(d["orderByConsumed"] for d in decisions)
        ok = consumed == spec["must_order_consumed"]
        notes.append({"order_consumed": consumed, "as_expected": ok})
        result["ok"] = result.get("ok", True) and ok

    if spec.get("unusable_constraint"):
        # outer 场景至少存在一次 unusable 约束，且它绝不能拿到 argvIndex。
        has_unusable = any(
            (not c["usable"]) for req in requests for c in req["constraints"]
        )
        no_argv_on_unusable = True
        for req, dec in zip(requests, decisions):
            for c, u in zip(req["constraints"], dec["usage"]):
                if not c["usable"] and u["argvIndex"] != 0:
                    no_argv_on_unusable = False
        notes.append(
            {
                "saw_unusable": has_unusable,
                "no_argv_on_unusable": no_argv_on_unusable,
            }
        )
        result["ok"] = result.get("ok", True) and has_unusable and no_argv_on_unusable

    if spec.get("max_rows_per_cursor") is not None:
        # id 等值的公开窄访问：每个远端游标候选行数不得超过命中行数。
        opens = [e for e in remote.page_log if e["event"] == "open"]
        ok = all(e["candidates"] <= spec["max_rows_per_cursor"] for e in opens)
        notes.append({"narrow_access": ok, "open_candidates": [e["candidates"] for e in opens]})
        result["ok"] = result.get("ok", True) and ok

    if spec.get("limit_not_pushed"):
        # 分页必须覆盖全部候选（源不知道 LIMIT），关闭数与打开数相等。
        stats = remote.stats()
        ok = stats["opens"] == stats["closes"] and not stats["active"]
        notes.append({"full_candidates_paged": ok, "stats": stats})
        result["ok"] = result.get("ok", True) and ok

    if spec.get("no_leak") or True:
        leaked = sorted(remote.active)
        stats = remote.stats()
        ok = not leaked and stats["opens"] == stats["closes"]
        notes.append({"no_leak": ok, "stats": stats})
        result["ok"] = result.get("ok", True) and ok

    if spec.get("prefix_candidate"):
        # 候选集合必须是最终 SQL 结果的超集（源只做前缀，内核做残余）。
        opens = [e for e in remote.page_log if e["event"] == "open"]
        notes.append({"candidate_opens": [{"filters": e["filters"], "candidates": e["candidates"]} for e in opens]})

    # 分页大小硬约束：任何一页都不得超过 PAGE_SIZE。
    oversize = [e for e in remote.page_log if e["event"] == "page" and e["rows"] > PAGE_SIZE]
    result["ok"] = result.get("ok", True) and not oversize
    notes.append({"page_size_respected": not oversize})

    result["invariants"] = notes


def run_one(name, rows):
    remote = make_remote(rows)
    oracle = make_oracle(rows)
    plans, restore = snapshot_plans()
    sql, bindings, spec = scenarios()[name]
    try:
        got, expected = run_case(remote, oracle, sql, bindings)
    finally:
        restore()
        oracle.close()

    result = {
        "scenario": name,
        "sql": sql,
        "actual": got,
        "expected": expected,
        "rows_match": got == expected,
        "plans": plans,
        "page_log": remote.page_log,
        "close_log": remote.close_log,
        "leaked_cursors": sorted(remote.active),
        "stats": remote.stats(),
    }
    check_invariants(result, plans, remote, spec, got, expected)
    result["ok"] = result.get("ok", True) and got == expected
    return result


# ---------------------------------------------------------------------- #
# 异常安全：查询中途出错也必须释放远端游标
# ---------------------------------------------------------------------- #
def run_error_case(rows):
    remote = make_remote(rows)
    db = connect(remote)
    error = None
    try:
        # 自定义函数在读取虚表行时抛错，模拟查询中途异常。
        db.createscalarfunction("boom", lambda x: 1 / 0)
        list(db.execute("select boom(id) from facts where id > 0"))
    except ZeroDivisionError as exc:
        error = type(exc).__name__
    finally:
        db.close()
    return {
        "scenario": "error",
        "error": error,
        "close_log": remote.close_log,
        "leaked_cursors": sorted(remote.active),
        "ok": error == "ZeroDivisionError" and not remote.active,
    }


# ---------------------------------------------------------------------- #
# 协商层单测：直接驱动 planner.choose，覆盖内核在本数据上不会自然产生的
# usable=False / IN / 异 collation 等组合（不模拟任何数据面回调）。
# ---------------------------------------------------------------------- #
class FakeIndexInfo:
    def __init__(self, constraints, orderby=(), collations=None, in_flags=()):
        self._c = constraints
        self._o = orderby
        self._coll = collations or ["BINARY"] * len(constraints)
        self._in = in_flags or [False] * len(constraints)
        self._usage = [[0, False, False] for _ in constraints]
        self.idxStr = None
        self.orderByConsumed = None
        self.estimatedRows = None
        self.estimatedCost = None

    @property
    def nConstraint(self):
        return len(self._c)

    @property
    def nOrderBy(self):
        return len(self._o)

    def get_aConstraint_iColumn(self, i):
        return self._c[i][0]

    def get_aConstraint_op(self, i):
        return self._c[i][1]

    def get_aConstraint_usable(self, i):
        return self._c[i][2]

    def get_aConstraintUsage_in(self, i):
        return self._in[i]

    def get_aConstraint_collation(self, i):
        return self._coll[i]

    def get_aOrderBy_iColumn(self, i):
        return self._o[i][0]

    def get_aOrderBy_desc(self, i):
        return self._o[i][1]

    def set_aConstraintUsage_argvIndex(self, i, v):
        self._usage[i][0] = v

    def set_aConstraintUsage_omit(self, i, v):
        self._usage[i][1] = v

    def get_aConstraintUsage_argvIndex(self, i):
        return self._usage[i][0]

    def get_aConstraintUsage_omit(self, i):
        return self._usage[i][1]


def run_planner_units():
    import json
    import planner
    from remote import FILTER_ID_EQ, FILTER_NAME_PREFIX

    EQ = apsw.SQLITE_INDEX_CONSTRAINT_EQ
    GT = apsw.SQLITE_INDEX_CONSTRAINT_GT
    LIKE = apsw.SQLITE_INDEX_CONSTRAINT_LIKE
    ISNULL = apsw.SQLITE_INDEX_CONSTRAINT_ISNULL
    checks = []

    def record(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # 1) unusable 的 id 等值必须被忽略；usable 的 name 等值只取前缀候选、不 omit。
    f = FakeIndexInfo(
        [(0, EQ, False), (1, EQ, True)],
        orderby=((2, False),),
    )
    planner.choose(f)
    plan = json.loads(f.idxStr)
    record(
        "unusable_id_eq_skipped",
        f._usage[0] == [0, False, False]
        and plan == [{"kind": FILTER_NAME_PREFIX, "argv": 1}]
        and f._usage[1] == [1, False, False],
        {"usage": f._usage, "plan": plan},
    )
    record("order_never_consumed", f.orderByConsumed is False, f.orderByConsumed)

    # 2) id 等值是唯一允许 omit 的约束，argv 次序随约束次序稳定递增。
    f = FakeIndexInfo([(0, EQ, True), (1, EQ, True)])
    planner.choose(f)
    plan = json.loads(f.idxStr)
    record(
        "id_eq_omit_and_stable_argv",
        f._usage[0] == [1, True, False]
        and f._usage[1] == [2, False, False]
        and [s["kind"] for s in plan] == [FILTER_ID_EQ, FILTER_NAME_PREFIX]
        and [s["argv"] for s in plan] == [1, 2],
        {"usage": f._usage, "plan": plan},
    )

    # 3) IN 列表、范围、IS NULL、LIKE 全部不下推：空计划、零 argv、零 omit。
    f = FakeIndexInfo(
        [(0, EQ, True), (2, GT, True), (2, ISNULL, True), (1, LIKE, True)],
        in_flags=[True, False, False, False],
    )
    planner.choose(f)
    record(
        "in_range_null_like_not_pushed",
        json.loads(f.idxStr) == []
        and all(u == [0, False, False] for u in f._usage),
        {"usage": f._usage, "idxStr": f.idxStr},
    )

    # 4) 未知 collation 的 name 等值不下推；NOCASE 可下推为候选但不 omit。
    f = FakeIndexInfo(
        [(1, EQ, True), (1, EQ, True)],
        collations=["RTRIM", "NOCASE"],
    )
    planner.choose(f)
    plan = json.loads(f.idxStr)
    record(
        "collation_negotiation",
        f._usage[0] == [0, False, False]
        and f._usage[1] == [1, False, False]
        and plan == [{"kind": FILTER_NAME_PREFIX, "argv": 1}],
        {"usage": f._usage, "plan": plan},
    )

    # 5) DESC 排序同样不能被宣称消费。
    f = FakeIndexInfo([], orderby=((2, True),))
    planner.choose(f)
    record("desc_order_not_consumed", f.orderByConsumed is False, f.orderByConsumed)

    # 6) 每次规划独立构造：连续两次 choose 不共享 argv 起点/旧选择。
    f1 = FakeIndexInfo([(0, EQ, True)])
    f2 = FakeIndexInfo([(1, EQ, True)])
    planner.choose(f1)
    planner.choose(f2)
    record(
        "fresh_plan_each_time",
        json.loads(f1.idxStr) == [{"kind": FILTER_ID_EQ, "argv": 1}]
        and json.loads(f2.idxStr) == [{"kind": FILTER_NAME_PREFIX, "argv": 1}],
        [f1.idxStr, f2.idxStr],
    )

    return {
        "scenario": "planner_units",
        "checks": checks,
        "ok": all(c["ok"] for c in checks),
    }


def main(argv):
    big = False
    names = argv[1:]
    if names and names[0] == "big":
        big = True
        names = names[1:]
    if not names or names == ["all"]:
        names = [n for n in scenarios()] + ["planner_units", "error"]

    rows = make_big_rows(3000) if big else SMALL_ROWS
    payload = {"dataset": "big-3000" if big else "small", "results": []}
    overall_ok = True
    for name in names:
        if name == "error":
            r = run_error_case(rows)
        elif name == "planner_units":
            r = run_planner_units()
        else:
            if name not in scenarios():
                raise SystemExit("unknown scenario: %s" % name)
            r = run_one(name, rows)
        payload["results"].append(r)
        overall_ok = overall_ok and r["ok"]
    payload["ok"] = overall_ok
    print(json.dumps(payload, sort_keys=True, indent=2))
    raise SystemExit(0 if overall_ok else 1)


if __name__ == "__main__":
    main(sys.argv)
