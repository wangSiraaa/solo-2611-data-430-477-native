"""把有限远端行源接成真实的 SQLite 虚表（只读）。

游标协议严格对应 SQLite 的 xFilter/xNext/xEof/xColumn/xClose：

* Filter 每次都按本次 argv 重新规划访问：先关闭上一轮残留的远端游标，
  再打开新的有状态分页游标，绝不跨执行沿用旧绑定或旧页；
* Next/Eof/Column 只操作当前 Filter 绑定的远端页，重复 Filter 后不会
  串用上一次绑定的页；
* 查询提前停止、中途异常或连接关闭都会释放远端游标；
* 一条查询里的两个虚表游标实例各自持有独立远端 cid，页位置互不共享。

约束的下推/omit 决策在 :mod:`planner` 完成，这里只负责按计划取值，
残余过滤与排序全部由 SQLite 内核完成，本模块不模拟任何内核回调。
"""

import json

import apsw

import planner

_SCHEMA = "create table x(id INTEGER, name TEXT, amount INTEGER)"


class Module:
    def __init__(self, remote):
        self.remote = remote

    def Create(self, *args):
        return _SCHEMA, Table(self.remote)

    Connect = Create


class Table:
    def __init__(self, remote):
        self.remote = remote

    def BestIndexObject(self, info):
        # 调用时再查找，便于探针观察真实的协商过程（生产行为不变）。
        return planner.choose(info)

    def Open(self):
        # 每个虚表游标拿一个独立的协议对象，远端 cid 也各自独立。
        return Cursor(self.remote)

    def Disconnect(self):
        # 连接关闭时 SQLite 会先关闭各游标；这里没有额外的表级资源。
        pass

    Destroy = Disconnect


class Cursor:
    def __init__(self, remote):
        self.remote = remote
        self._cid = None          # 当前 Filter 打开的远端游标
        self._page = []           # 当前页
        self._row = None          # 当前行
        self._at = 0              # 页内下标
        self._eof = True

    # ------------------------------------------------------------------ #
    def _release_remote(self):
        """关闭当前远端游标（若有）。幂等，异常安全。"""
        if self._cid is not None:
            cid = self._cid
            self._cid = None
            try:
                self.remote.close(cid)
            except Exception:
                # 释放路径上的异常不能掩盖正常查询结果或原始异常。
                pass

    def Filter(self, number, string, values):
        # 同一虚表游标可能被连接（join）重复 Filter：先释放上一轮远端
        # 游标，避免泄漏；随后按本次 argv 重新打开，绝不复用旧绑定/旧页。
        self._release_remote()
        plan = json.loads(string) if string else []

        filters = []
        for step in plan:
            value = values[step["argv"] - 1]
            filters.append((step["kind"], value))

        try:
            cid = self.remote.open(filters)
        except BaseException:
            self._cid = None
            raise

        self._cid = cid
        self._page = []
        self._row = None
        self._at = 0
        self._eof = False
        self._advance()

    # ------------------------------------------------------------------ #
    def _advance(self):
        """从远端逐页推进，直到拿到下一行或确认 EOF。"""
        while True:
            if self._at < len(self._page):
                self._row = self._page[self._at]
                return
            if self._cid is None:
                self._eof = True
                self._row = None
                return
            page = self.remote.fetch_page(self._cid)
            self._page = page
            self._at = 0
            if not page:
                # 源端已到末尾：立刻释放远端游标，查询提前停止时也不泄漏。
                self._release_remote()
                self._eof = True
                self._row = None
                return

    def Eof(self):
        return self._eof

    def Rowid(self):
        return self._row[0]

    def Column(self, col):
        return self._row[col]

    def Next(self):
        if self._eof:
            return
        self._at += 1
        self._advance()

    def Close(self):
        # 连接关闭 / 游标回收：必须释放远端分页游标。
        self._release_remote()
        self._page = []
        self._row = None
        self._eof = True


def connect(remote):
    db = apsw.Connection(":memory:")
    db.create_module(
        "source", Module(remote), use_bestindex_object=True, read_only=True
    )
    db.execute("create virtual table facts using source")
    return db
