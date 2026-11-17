"""SQLite 持久化层：只负责不可变版本的存储与事务边界。

每个发布版本保存三样东西：
  body    —— 规范化后的规范正文（ref 仍带 subject/精确 version，不嵌入解析结果）
  closure —— 全部引用解析闭包，排序后的 [[subject, version], ...]
  digest  —— 对 body+closure 的摘要，同 subject 内唯一，作为幂等重试的数据库级保证

Registry 是唯一允许写入的入口；Catalog 自身不做任何兼容/校验决策。
"""
import contextlib
import hashlib
import json
import sqlite3
import threading


def canonical_json(obj):
    """规范化序列化：键排序、无空白，作为去重与摘要的唯一标准形式。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_of(body, closure):
    payload = canonical_json({"body": body, "closure": sorted(closure)})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_DDL = [
    """
    create table if not exists schemas(
        subject    text not null,
        version    integer not null,
        body       text not null,
        closure    text not null,
        digest     text not null,
        created_at text not null default (datetime('now')),
        primary key (subject, version)
    )
    """,
    "create unique index if not exists idx_schemas_digest on schemas(subject, digest)",
]


class Catalog:
    def __init__(self, path=":memory:", uri=False):
        # check_same_thread=False：并发发布测试需要多线程共享同一连接文件/共享缓存库
        self.db = sqlite3.connect(path, uri=uri, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=10000")
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass  # 内存库不支持 WAL
        for stmt in _DDL:
            self.db.execute(stmt)
        self.db.commit()
        # 单连接被多线程共享时，sqlite3 模块不保证并发安全：进程内写操作先串行化；
        # BEGIN IMMEDIATE 再为多连接/文件库的跨进程竞争提供数据库级串行化。
        self._write_lock = threading.Lock()

    # ---- 事务边界 -------------------------------------------------
    @contextlib.contextmanager
    def write_txn(self):
        """串行化的写事务：同一时刻只允许一个注册流程在读历史、做检查并发布。"""
        with self._write_lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    # ---- 读 -------------------------------------------------------
    def count_subjects(self):
        return self.db.execute("select count(distinct subject) from schemas").fetchone()[0]

    def subject_exists(self, subject):
        return self.db.execute(
            "select 1 from schemas where subject=? limit 1", (subject,)
        ).fetchone() is not None

    def count_versions(self, subject):
        return self.db.execute(
            "select count(*) from schemas where subject=?", (subject,)
        ).fetchone()[0]

    def all_versions(self, subject):
        rows = self.db.execute(
            "select version from schemas where subject=? order by version", (subject,)
        ).fetchall()
        return [r[0] for r in rows]

    def latest_version(self, subject):
        row = self.db.execute(
            "select max(version) from schemas where subject=?", (subject,)
        ).fetchone()
        return row[0]

    def find_by_digest(self, subject, digest):
        row = self.db.execute(
            "select version from schemas where subject=? and digest=?", (subject, digest)
        ).fetchone()
        return None if row is None else row[0]

    def get_contract(self, subject, version):
        """返回不可变契约行：正文、闭包、摘要。永远不会混入后来发布的 latest。"""
        row = self.db.execute(
            "select subject, version, body, closure, digest from schemas "
            "where subject=? and version=?",
            (subject, version),
        ).fetchone()
        if row is None:
            raise KeyError((subject, version))
        return {
            "subject": row["subject"],
            "version": row["version"],
            "body": json.loads(row["body"]),
            "closure": [tuple(p) for p in json.loads(row["closure"])],
            "digest": row["digest"],
        }

    def get(self, subject, version=None):
        """兼容旧入口的 (version, body) 形态；version 缺省取 latest，但只返回该版本自身。"""
        if version is None:
            version = self.latest_version(subject)
            if version is None:
                raise KeyError((subject, None))
        contract = self.get_contract(subject, version)
        return contract["version"], contract["body"]

    # ---- 写（仅允许在 write_txn 内调用） --------------------------
    def put(self, subject, version, body, closure, digest):
        self.db.execute(
            "insert into schemas(subject, version, body, closure, digest) values(?,?,?,?,?)",
            (subject, version, canonical_json(body), canonical_json(sorted(closure)), digest),
        )

    def close(self):
        self.db.close()
