"""有限的远端行源。

固定 schema: id INTEGER, name TEXT, amount INTEGER（各列允许 NULL）。

源的**真实**能力只有三种，且都不等同于任意 SQLite 表达式语义：

* id 精确等值（Python 相等语义，与 SQLite 数值亲和 + BINARY 比较一致）；
* casefold 名称**前缀候选**——只是候选集合，可能是 SQL 精确等值结果的
  超集，残余过滤必须由 SQLite 内核完成；
* amount 的**文本**升序（按 ``str(amount)`` 排序），因此绝不能被虚表模块
  宣称为 SQLite 的 INTEGER ORDER BY。

游标是有状态、分页的：每次 ``fetch_page`` 至多返回 2 行，游标在
``close`` 之前一直保留自己的页位置。不同游标互不共享位置。
"""

import re

# 列下标，与固定 schema 对应。
COL_ID = 0
COL_NAME = 1
COL_AMOUNT = 2

# 远端过滤类型。
FILTER_ID_EQ = "id_eq"
FILTER_NAME_PREFIX = "name_prefix"

PAGE_SIZE = 2

# SQLite 数值字面量（数字亲和可转换的文本形态）：
# 可选符号 + 整数/小数部分 + 可选指数；前后空白由 strip 处理。
_NUMERIC_LITERAL = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\Z")


def _affinity_integer(value):
    """按 SQLite INTEGER 亲和把绑定值规整为整数；不可规整返回 None。

    依据内核实测的比较规则：

    * int（含 bool）原样/转 int；
    * float 仅当为整数值（如 2.0）时转 int，1.5 这类返回 None；
    * 文本去除前后空白后，必须是合法数值字面量（整数、小数如 '0.0'、
      指数如 '1e1'、'1.0e1'）才按数字亲和转换，且只有转换结果是整数值
      （如 '1.5'、'1e-1' 得到非整数）时才返回整数；'1abc'、'0x1'、
      'e1'、'' 等非法字面量返回 None；
    * None（SQL NULL）返回 None，由调用方得到空结果（``id = NULL`` 未知）。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, bytes):
        text = value.decode("utf-8", "ignore")
    else:
        text = str(value)
    text = text.strip()
    if not _NUMERIC_LITERAL.match(text):
        return None
    numeric = float(text)
    if not numeric.is_integer():
        return None
    return int(numeric)


class RemoteCursorClosedError(RuntimeError):
    """对已关闭的远端游标取页时抛出。"""


class Remote:
    def __init__(self, rows=None):
        # 初始演示数据；测试可用 replace_rows 换成至多 3000 行的合法新数据。
        if rows is None:
            rows = [(1, "ann", 2), (2, "anne", 10), (3, "bob", 3), (4, "ann", None)]
        self._rows = list(rows)
        self.serial = 0
        # cid -> 游标私有状态（候选行、下一行下标）。游标之间不共享位置。
        self._cursors = {}
        # 可观察的取页日志与关闭记录。
        self.page_log = []
        self.close_log = []

    # ------------------------------------------------------------------ #
    # 数据装载（源可被换成合法新数据：id 唯一、行数有限）
    # ------------------------------------------------------------------ #
    def replace_rows(self, rows):
        rows = list(rows)
        ids = [r[COL_ID] for r in rows if r[COL_ID] is not None]
        if len(ids) != len(set(ids)):
            raise ValueError("remote rows must have unique non-NULL id")
        self._rows = rows

    @property
    def rows(self):
        return list(self._rows)

    def __len__(self):
        return len(self._rows)

    # ------------------------------------------------------------------ #
    # 过滤与排序：只实现源真实具备的能力
    # ------------------------------------------------------------------ #
    def _apply(self, filters):
        rows = self._rows
        for kind, value in filters:
            if kind == FILTER_ID_EQ:
                # 源能力：id 精确等值。id 列是 INTEGER 亲和，需复刻 SQLite 的
                # 亲和比较：可表示为整数的文本绑定（如 '1'）按整数比较，
                # 无法转换的文本（如 'abc'）不匹配任何行。value 为 None 时
                # 没有行能满足 SQL 的 ``id = NULL``（NULL 比较结果未知）。
                numeric = _affinity_integer(value)
                if numeric is None:
                    rows = []
                else:
                    rows = [
                        r
                        for r in rows
                        if r[COL_ID] is not None and r[COL_ID] == numeric
                    ]
            elif kind == FILTER_NAME_PREFIX:
                # 源能力：casefold 名称前缀候选。这是候选而非等值结果，
                # 必须是 SQL 结果的超集（不得漏行），残余等值由内核过滤。
                if value is None:
                    rows = []
                else:
                    prefix = str(value).casefold()
                    rows = [
                        r
                        for r in rows
                        if r[COL_NAME] is not None
                        and r[COL_NAME].casefold().startswith(prefix)
                    ]
            else:
                raise ValueError("unsupported remote filter: %r" % (kind,))
        # 源能力：文本金额升序。注意这不是 SQLite 的 INTEGER 排序，
        # 仅作为源自身的输出顺序，planner 不得据此宣称 orderByConsumed。
        return sorted(rows, key=lambda r: str(r[COL_AMOUNT]))

    # ------------------------------------------------------------------ #
    # 有状态分页游标协议
    # ------------------------------------------------------------------ #
    def open(self, filters):
        """打开一个远端游标，返回 cid。

        游标持有按源能力过滤/排序后的候选行快照与独立页位置，
        之后通过 :meth:`fetch_page` 逐页读取。
        """
        self.serial += 1
        cid = self.serial
        snapshot = self._apply(list(filters))
        self._cursors[cid] = {"rows": snapshot, "pos": 0, "filters": list(filters)}
        self.page_log.append(
            {
                "event": "open",
                "cursor": cid,
                "filters": [[k, v] for k, v in filters],
                "candidates": len(snapshot),
            }
        )
        return cid

    def fetch_page(self, cid):
        """读取下一页，至多 :data:`PAGE_SIZE` 行；到末尾后返回 ``[]``。"""
        state = self._cursors.get(cid)
        if state is None:
            raise RemoteCursorClosedError("remote cursor %s is not open" % (cid,))
        page = state["rows"][state["pos"] : state["pos"] + PAGE_SIZE]
        state["pos"] += len(page)
        self.page_log.append(
            {
                "event": "page",
                "cursor": cid,
                "rows": len(page),
                "next_pos": state["pos"],
                "eof": state["pos"] >= len(state["rows"]),
            }
        )
        return page

    def close(self, cid):
        """释放远端游标。幂等：重复关闭以及关闭未知 cid 都可观察且安全。"""
        self.close_log.append(cid)
        self._cursors.pop(cid, None)

    # ------------------------------------------------------------------ #
    # 自检辅助
    # ------------------------------------------------------------------ #
    @property
    def active(self):
        """仍未关闭的远端游标 cid 集合。"""
        return set(self._cursors)

    def stats(self):
        return {
            "opens": sum(1 for e in self.page_log if e["event"] == "open"),
            "pages": sum(1 for e in self.page_log if e["event"] == "page"),
            "closes": len(self.close_log),
            "active": sorted(self._cursors),
        }
