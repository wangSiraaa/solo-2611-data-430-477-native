"""虚表访问协商（xBestIndex）。

把 SQLite 内核给出的约束/排序请求与 :mod:`remote` 的真实能力做协商，
协商结果通过 ``info.idxStr``（JSON）交给游标 Filter。

可完整下推的约束只有：

* ``id = ?``：源做精确等值且与 SQLite 语义一致，可 omit；
* ``name = ?``（BINARY/NOCASE，单值）：源只能给 casefold **前缀候选**，
  是 SQL 等值结果的超集，因此绑定 argv 下推取候选，但**绝不 omit**，
  残余等值比较交回 SQLite 内核，保证不漏行也不多行。

其余一切（id/amount 范围、IS NULL、LIKE/GLOB、IN 列表、其他 collation、
名称的非等值比较等）都无法被源完整执行：不绑定、不 omit，全部留作内核
残余计算，虚表只做全量候选扫描。

排序方面，源只提供 amount 的**文本**升序，与 SQLite INTEGER 升序语义
不同（例如 10 与 2 的次序、NULL 的位置），所以任何 ORDER BY 都不能宣称
orderByConsumed，一律交回内核排序。
"""

import json

import apsw

from remote import (
    COL_ID,
    COL_NAME,
    FILTER_ID_EQ,
    FILTER_NAME_PREFIX,
)

# 源的 casefold 前缀候选只能覆盖这两种内建 collation 下的等值约束；
# 即便如此也只是候选，残余比较仍由内核完成。
_PREFIX_CAPABLE_COLLATIONS = ("BINARY", "NOCASE")


def choose(info):
    # plan 元素与 Filter 的 argv 次序一一对应；每次规划都从零构造，
    # 不保留也不沿用任何历史执行的绑定值。
    plan = []
    argv = 1
    id_equal = False

    for i in range(info.nConstraint):
        # 只使用 usable 约束；outer join 右表上不可用的约束必须原样留给内核。
        if not info.get_aConstraint_usable(i):
            continue

        column = info.get_aConstraint_iColumn(i)
        op = info.get_aConstraint_op(i)
        if op != apsw.SQLITE_INDEX_CONSTRAINT_EQ:
            continue
        # IN 列表（rhs 为多值）源不支持，跳过，交由内核处理。
        if info.get_aConstraintUsage_in(i):
            continue

        if column == COL_ID:
            # id 精确等值是源能完整执行的约束：绑定 argv 并允许 omit。
            info.set_aConstraintUsage_argvIndex(i, argv)
            info.set_aConstraintUsage_omit(i, True)
            plan.append({"kind": FILTER_ID_EQ, "argv": argv})
            argv += 1
            id_equal = True

        elif column == COL_NAME:
            collation = info.get_aConstraint_collation(i)
            if collation in _PREFIX_CAPABLE_COLLATIONS:
                # 源只能给 casefold 前缀候选（可能是等值结果的超集）：
                # 可以绑定 argv 缩小候选，但 omit 必须保持 False，
                # 由 SQLite 内核对候选行补做完整的等值/排序规则比较。
                info.set_aConstraintUsage_argvIndex(i, argv)
                plan.append({"kind": FILTER_NAME_PREFIX, "argv": argv})
                argv += 1

        # 其它列（如 amount）或不认识的约束：不触碰 usage，内核自行残余处理。

    info.idxStr = json.dumps(plan)

    # 代价/行数估计：id 等值是窄访问；其余都是候选扫描。
    if id_equal:
        info.estimatedRows = 1
        info.estimatedCost = 10.0
    else:
        info.estimatedRows = 1000
        info.estimatedCost = 1000.0

    # 源没有任何与 SQLite 排序语义完全一致的排序能力，排序永远交回内核。
    info.orderByConsumed = False
    return True
