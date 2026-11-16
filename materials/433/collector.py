import json
import sqlite3
import threading

from service import page_digest

TRUNCATED_RETRIES = 5


class TruncatedPage(Exception):
    pass


class PageContentConflict(Exception):
    """同 page_id 重放得到异文：已提交内容不被覆盖。"""


class StreamFailed(Exception):
    pass


class Collector:
    """可恢复采集链。

    提交语义：一页的 page_id / rows / next / digest / 游标在同一个 SQLite
    事务中原子提交；截断页（complete=false）不提交、游标不前进，重放完整
    页后按 page_id 幂等处理，不重复落行。流之间状态独立，一个流的数据
    错误不会删除另一个流已提交的页。重新构造 Collector 即从已提交页继续。
    """

    def __init__(self, requester, db_path=":memory:"):
        self.requester = requester
        self.db_path = db_path
        # 单连接跨线程共享（:memory: 库本就每连接独立，无法用每线程连接）；
        # 锁只包住本地 SQLite 读写，GET/rotate 在锁外，采集仍真正并行。
        self._db_lock = threading.RLock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _conn(self):
        return self._db

    def _init_schema(self):
        conn = self._conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS streams(
                stream TEXT PRIMARY KEY,
                cursor INTEGER,
                status TEXT NOT NULL,
                failure_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS pages(
                stream TEXT NOT NULL,
                seq INTEGER NOT NULL,
                page_id TEXT NOT NULL UNIQUE,
                next_cursor INTEGER,
                digest TEXT NOT NULL,
                PRIMARY KEY(stream, seq)
            );
            CREATE TABLE IF NOT EXISTS page_rows(
                stream TEXT NOT NULL,
                seq INTEGER NOT NULL,
                row_idx INTEGER NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(stream, seq, row_idx)
            );
            """
        )
        conn.commit()

    # ---------- 状态读取 ----------

    def stream_state(self, stream):
        cur = self._conn().execute(
            "SELECT cursor, status, failure_reason FROM streams WHERE stream=?",
            (stream,),
        )
        row = cur.fetchone()
        if row is None:
            return {"cursor": 0, "status": "new", "failure_reason": None}
        return {"cursor": row[0], "status": row[1], "failure_reason": row[2]}

    def rows(self, stream):
        cur = self._conn().execute(
            "SELECT payload FROM page_rows WHERE stream=? ORDER BY seq, row_idx",
            (stream,),
        )
        return [json.loads(r[0]) for r in cur.fetchall()]

    def committed_pages(self, stream):
        cur = self._conn().execute(
            "SELECT page_id, next_cursor, digest FROM pages WHERE stream=? ORDER BY seq",
            (stream,),
        )
        return [
            {"page_id": r[0], "next": r[1], "digest": r[2]} for r in cur.fetchall()
        ]

    # ---------- 采集 ----------

    def collect(self, stream, max_pages=None):
        state = self.stream_state(stream)
        if state["status"] == "failed":
            raise StreamFailed(f"{stream} failed: {state['failure_reason']}")
        cursor = state["cursor"]
        fetched = 0
        while cursor is not None and (max_pages is None or fetched < max_pages):
            page = self._fetch_complete(stream, cursor)
            cursor = self._commit_page(stream, cursor, page)
            fetched += 1
        if cursor is None and self.stream_state(stream)["status"] != "failed":
            self._mark_status(stream, "done")
        return self.rows(stream)

    def _fetch_complete(self, stream, cursor):
        """GET 可安全重放：截断页不前进游标，反复 GET 直到拿到完整页。"""
        last = None
        for _ in range(TRUNCATED_RETRIES):
            page = self.requester.page(stream, cursor)
            last = page
            if page.get("complete"):
                return page
        raise TruncatedPage(
            f"{stream}:{cursor} still incomplete after {TRUNCATED_RETRIES} GETs: {last}"
        )

    def _commit_page(self, stream, cursor, page):
        page_id = page["page_id"]
        rows = page["rows"]
        nxt = page["next"]
        expected_digest = page_digest(page_id, rows, nxt)
        advertised = page.get("digest")
        if advertised is not None and advertised != expected_digest:
            self._fail(
                stream,
                cursor,
                f"digest mismatch for {page_id}: advertised {advertised} "
                f"!= computed {expected_digest}",
            )
            raise PageContentConflict(f"digest mismatch for {page_id}")

        conn = self._conn()
        with self._db_lock:
            cur = conn.execute(
                "SELECT seq, digest, next_cursor FROM pages WHERE page_id=?",
                (page_id,),
            )
            existing = cur.fetchone()
            if existing is not None:
                seq, stored_digest, stored_next = existing
                # 重放检查服务于重放，不做一般内容去重：同 page_id 必须同文。
                if stored_digest != expected_digest:
                    self._fail(
                        stream,
                        cursor,
                        f"page_id {page_id} replayed with divergent content "
                        f"(stored {stored_digest} != replay {expected_digest})",
                    )
                    raise PageContentConflict(
                        f"divergent replay for committed page {page_id}"
                    )
                # 同文幂等：不重复落行，游标保持已提交位置
                return stored_next
            cur = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM pages WHERE stream=?",
                (stream,),
            )
            seq = cur.fetchone()[0]
            if seq != cursor:
                self._fail(
                    stream,
                    cursor,
                    f"page order violation for {stream}: expected seq {cursor}, got {seq}",
                )
                raise PageContentConflict(
                    f"unexpected page position for {page_id}"
                )
            try:
                conn.execute("BEGIN")
                conn.execute(
                    "INSERT INTO pages(stream, seq, page_id, next_cursor, digest)"
                    " VALUES(?,?,?,?,?)",
                    (stream, seq, page_id, nxt, expected_digest),
                )
                conn.executemany(
                    "INSERT INTO page_rows(stream, seq, row_idx, payload)"
                    " VALUES(?,?,?,?)",
                    [
                        (stream, seq, idx, json.dumps(cell, ensure_ascii=False))
                        for idx, cell in enumerate(rows)
                    ],
                )
                conn.execute(
                    "INSERT INTO streams(stream, cursor, status, failure_reason)"
                    " VALUES(?,?,'active',NULL)"
                    " ON CONFLICT(stream) DO UPDATE SET cursor=excluded.cursor,"
                    " status='active', failure_reason=NULL",
                    (stream, nxt),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return nxt

    # ---------- 失败处理（流间隔离） ----------

    def retry(self, stream):
        """显式恢复一个 failed 流：已提交页与游标保留，仅清除失败标记。

        与认证域的 Auth.reset() 配合：先重新认证，再 retry 对应流。
        其他流的状态不受影响。
        """
        conn = self._conn()
        with self._db_lock:
            cur = conn.execute(
                "SELECT status FROM streams WHERE stream=?", (stream,)
            )
            row = cur.fetchone()
            if row is None or row[0] != "failed":
                return False
            conn.execute(
                "UPDATE streams SET status='active', failure_reason=NULL"
                " WHERE stream=?",
                (stream,),
            )
            conn.commit()
            return True

    def _mark_status(self, stream, status, reason=None):
        conn = self._conn()
        with self._db_lock:
            conn.execute(
                "INSERT INTO streams(stream, cursor, status, failure_reason)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(stream) DO UPDATE SET status=excluded.status,"
                " failure_reason=excluded.failure_reason",
                (stream, self.stream_state(stream)["cursor"], status, reason),
            )
            conn.commit()

    def _fail(self, stream, cursor, reason):
        conn = self._conn()
        with self._db_lock:
            conn.execute(
                "INSERT INTO streams(stream, cursor, status, failure_reason)"
                " VALUES(?,?,'failed',?)"
                " ON CONFLICT(stream) DO UPDATE SET status='failed',"
                " failure_reason=excluded.failure_reason",
                (stream, cursor, reason),
            )
            conn.commit()

    def collect_guarded(self, stream, max_pages=None):
        """采集并把失败原因落库，返回 (ok, payload)；不影响其他流。"""
        try:
            return True, self.collect(stream, max_pages=max_pages)
        except Exception as exc:
            if self.stream_state(stream)["status"] != "failed":
                self._fail(
                    stream,
                    self.stream_state(stream)["cursor"],
                    f"{type(exc).__name__}: {exc}",
                )
            return False, f"{type(exc).__name__}: {exc}"
