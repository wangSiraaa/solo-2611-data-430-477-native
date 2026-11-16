import json
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from auth import Auth, ReauthenticationRequired
from collector import Collector
from requester import Requester
from service import Source

FAILURES = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def new_chain(db_path=None):
    source = Source()
    auth = Auth(source)
    collector = Collector(Requester(source, auth), db_path or tempfile.mktemp(suffix=".db"))
    return source, auth, collector


# ---------------------------------------------------------------- normal
def scenario_normal():
    print("== normal ==")
    source, auth, collector = new_chain()
    t0 = time.time()
    with ThreadPoolExecutor(3) as ex:
        outs = list(ex.map(collector.collect_guarded, ["A", "B", "C"]))
    elapsed = time.time() - t0
    ok_all = all(o[0] for o in outs)
    expected = {
        "A": ["a1", "a2", "a3", "a4", "a5", "a6"],
        "B": ["b1", "b2", "b3", "b4", "b5", "b6"],
        "C": ["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"],
    }
    got = {s: collector.rows(s) for s in expected}
    check("all streams ok", ok_all, outs)
    check("row sets exact", got == expected, json.dumps(got, ensure_ascii=False))
    check("cursors exhausted", all(collector.stream_state(s)["cursor"] is None for s in expected))
    check("zero refresh calls", source.refresh_calls == 0, source.refresh_log)
    check("stream status done", all(collector.stream_state(s)["status"] == "done" for s in expected))

    # 并行性：3 流各 4 页，每页带延迟，串行耗时远大于并行
    source, auth, collector = new_chain()
    source.get_delay = 0.05
    t0 = time.time()
    with ThreadPoolExecutor(3) as ex:
        list(ex.map(collector.collect_guarded, ["A", "B", "C"]))
    elapsed = time.time() - t0
    serial_floor = 12 * 0.05
    check("GETs run in parallel (no collection-wide lock)", elapsed < serial_floor * 0.75,
          f"elapsed={elapsed:.3f}s serial_floor={serial_floor:.3f}s")


# ---------------------------------------------------------------- concurrent 401
def scenario_concurrent():
    print("== concurrent: two simultaneous 401 on one access generation ==")
    source, auth, collector = new_chain()
    # 两个流的第一页同时以同一旧代 access 撞 401（固定注入，可重复）。
    # b_get 保证两个 GET 都已发出并收到 401；rotate 门闩保证在两个 401
    # 都进入 Auth 之前不会开始轮换，从而精确复刻题面竞态。
    source.force_401_once = {("A", 0), ("B", 0)}
    b_get = threading.Barrier(2)
    b_401 = threading.Barrier(2)
    # 在两个同代 401 都进入 Auth 之前放行任何一个：精确复刻题面竞态，
    # 且不依赖 rotate 被调用几次（正确实现下只会有一次）。
    original_refresh = auth.refresh_after_401

    def gated_refresh(used):
        b_401.wait()
        return original_refresh(used)

    auth.refresh_after_401 = gated_refresh

    def worker(stream):
        b_get.wait()
        return collector.collect_guarded(stream)

    with ThreadPoolExecutor(2) as ex:
        outs = list(ex.map(worker, ["A", "B"]))
    check("both streams recovered and collected",
          all(o[0] for o in outs), outs)
    check("exactly one real rotate call", source.refresh_calls == 1, source.refresh_log)
    check("rotate used r0 once", [e["used"] for e in source.refresh_log] == ["r0"])
    check("generation advanced once", source.generation == 1 and auth.generation == 1)
    check("both streams share new access a1",
          auth.token() == "a1" and all(t == "a1" for (_, _, t) in source.get_log),
          source.get_log)
    check("no InvalidRefresh",
          all(e["outcome"] == "ok" for e in source.refresh_log), source.refresh_log)
    check("rows intact",
          collector.rows("A") == ["a1", "a2", "a3", "a4", "a5", "a6"]
          and collector.rows("B") == ["b1", "b2", "b3", "b4", "b5", "b6"])

    # 自然迟到 401：一个以旧代 a0 发出的请求，在轮换完成后才返回 401。
    # 先让 force_401 被一个旧代请求消费、但在 GET 内挂住，再由其他调用者完成轮换。
    source2, auth2, collector2 = new_chain()
    holder = threading.Event()
    release = threading.Event()

    def delayed_get(stream, page, token, expired=False):
        if (stream, page) in source2.force_401_once:
            source2.force_401_once.discard((stream, page))
            holder.set()
            release.wait(0.5)
            from service import Unauthorized
            raise Unauthorized(token)
        return Source.get(source2, stream, page, token, expired)

    source2.get = delayed_get
    source2.force_401_once = {("C", 0)}
    late_result = {}

    def late_call():
        late_result["page"] = collector2.requester.page("C", 0, token="a0")

    t = threading.Thread(target=late_call)
    t.start()
    holder.wait(1.0)                       # 旧代 GET 已挂起、尚未返回 401
    rotated = auth2.refresh_after_401("a0")  # 其他调用者先完成 a0 -> a1
    release.set()
    t.join()
    check("other caller rotated to a1 first", rotated == "a1")
    check("late stale 401 consumes no additional refresh",
          source2.refresh_calls == 1, source2.refresh_log)
    check("late 401 replayed with current generation",
          late_result["page"]["page_id"] == "C:0" and auth2.generation == 1,
          late_result)


# ---------------------------------------------------------------- late 401 (focused)
def scenario_late_401():
    print("== late 401 after another caller already rotated ==")
    source, auth, collector = new_chain()
    old_token = auth.token()  # a0
    # 其他调用者先完成一轮轮换
    first = auth.refresh_after_401(old_token)
    calls_after_first = source.refresh_calls
    # 迟到请求带着旧 access 回来处理 401
    shared = auth.refresh_after_401(old_token)
    check("first rotation produced a1", first == "a1")
    check("late 401 consumes no new refresh",
          source.refresh_calls == calls_after_first == 1, source.refresh_log)
    check("late waiter shares current token", shared == "a1")
    check("auth never rewound", auth.generation == 1 and auth.token() == "a1")


# ---------------------------------------------------------------- truncation + replay + resume
def scenario_truncated():
    print("== truncated page replay and resumable reconstruction ==")
    db_path = tempfile.mktemp(suffix=".db")
    source, auth, collector = new_chain(db_path)
    source.truncate_once = {("A", 1)}  # A:1 首次 GET 只给前缀
    ok, payload = collector.collect_guarded("A", max_pages=2)
    check("partial run ok", ok, payload)
    gets_for_a1 = [g for g in source.get_log if g[:2] == ("A", 1)]
    check("truncated page was GET twice", len(gets_for_a1) == 2, gets_for_a1)
    check("cursor stopped at 2 (truncated page never advanced early)",
          collector.stream_state("A")["cursor"] == 2)
    check("no duplicate rows from replay",
          collector.rows("A") == ["a1", "a2", "a3"], collector.rows("A"))

    # 模拟进程重建：全新 Collector 指向同一 SQLite，从已提交页继续
    source2 = Source()
    auth2 = Auth(source2)
    rebuilt = Collector(Requester(source2, auth2), db_path)
    state = rebuilt.stream_state("A")
    check("reconstructed collector resumes at cursor 2", state["cursor"] == 2, state)
    rebuilt.collect_guarded("A")
    check("resume completes full row set without duplicates",
          rebuilt.rows("A") == ["a1", "a2", "a3", "a4", "a5", "a6"],
          rebuilt.rows("A"))
    check("page order committed sequentially",
          [p["page_id"] for p in rebuilt.committed_pages("A")]
          == ["A:0", "A:1", "A:2", "A:3"])
    os.unlink(db_path)


# ---------------------------------------------------------------- divergent replay
def scenario_conflict():
    print("== same page_id replayed with divergent content ==")
    source, auth, collector = new_chain()
    ok0, _ = collector.collect_guarded("A", max_pages=1)
    # 重放 A:0 但行内容异文（同 page_id、不同摘要）
    source.replay_once = {("A", 1): (0, ["TAMPERED"])}
    ok, err = collector.collect_guarded("A")
    check("divergent replay fails the stream", not ok
          and "PageContentConflict" in err, err)
    reason = collector.stream_state("A")["failure_reason"]
    check("failure reason persisted", reason and "divergent" in reason, reason)
    check("committed page untouched",
          collector.rows("A") == ["a1", "a2"] and collector.stream_state("A")["cursor"] == 1)
    # 另一流不受影响
    ok_b, _ = collector.collect_guarded("B")
    check("other stream unaffected by failure isolation",
          ok_b and collector.rows("B") == ["b1", "b2", "b3", "b4", "b5", "b6"])

    # 同文重放必须幂等（服务 at-least-once 重放）
    source2, auth2, c2 = new_chain()
    c2.collect_guarded("B", max_pages=1)
    source2.replay_once = {("B", 1): (0, None)}  # 与 B:0 完全同文
    ok, err = c2.collect_guarded("B")
    check("identical replay is idempotent",
          ok and c2.rows("B") == ["b1", "b2", "b3", "b4", "b5", "b6"], err)


# ---------------------------------------------------------------- unknown rotation outcome
def scenario_unknown():
    print("== refresh response lost after request was sent ==")
    source, auth, collector = new_chain()
    source.lost_refresh = True
    raised = None
    try:
        auth.refresh_after_401(auth.token())
    except ReauthenticationRequired as exc:
        raised = exc
    check("lost outcome raises ReauthenticationRequired", raised is not None, str(raised))
    check("no blind retry: exactly one rotate call",
          source.refresh_calls == 1, source.refresh_log)
    check("auth domain marked reauth_required", auth.snapshot()["reauth_required"])
    check("no fabricated token: access unchanged", auth.snapshot()["access"] == "a0")
    # 同代等待者一致失败
    late_err = None
    try:
        auth.refresh_after_401("a0")
    except ReauthenticationRequired as exc:
        late_err = exc
    check("same-generation waiters fail consistently", late_err is not None)
    # 页面请求也一致停止，且不会再碰 refresh
    page_err = None
    try:
        collector.requester.page("A", 0)
    except ReauthenticationRequired as exc:
        page_err = exc
    check("page requests stop without consuming another refresh",
          page_err is not None and source.refresh_calls == 1, source.refresh_log)
    # 显式重新认证后恢复（服务端实际已轮换到 r1）
    auth.reset(access=source.access, refresh=source.refresh)
    collector.retry("A")
    rows = collector.collect("A")
    check("explicit reset resumes collection",
          rows == ["a1", "a2", "a3", "a4", "a5", "a6"], rows)


# ---------------------------------------------------------------- definitive rejection
def scenario_reject():
    print("== refresh definitively rejected ==")
    source, auth, collector = new_chain()
    source.reject_refresh = True
    err = None
    try:
        auth.refresh_after_401(auth.token())
    except ReauthenticationRequired as exc:
        err = exc
    check("rejection surfaces as ReauthenticationRequired", err is not None, str(err))
    check("exactly one rotate attempt, marked invalid",
          source.refresh_calls == 1 and source.refresh_log[0]["outcome"] == "invalid",
          source.refresh_log)
    check("domain stopped, no fake token",
          auth.snapshot()["reauth_required"] and auth.snapshot()["access"] == "a0")
    # 旧 refresh 不会被盲试第二次（经 Collector 走完整失败落库路径）
    ok_b, err_b = collector.collect_guarded("B")
    check("one-shot refresh never retried", source.refresh_calls == 1, source.refresh_log)
    check("stream failure persisted with reason",
          not ok_b and collector.stream_state("B")["status"] == "failed", err_b)
    # 重新认证 + 流重试：failed 流从游标 0 继续，已提交的其他流不受影响
    source.reject_refresh = False
    auth.reset(access=source.access, refresh=source.refresh)
    check("retry clears failed stream", collector.retry("B") is True)
    ok, rows = collector.collect_guarded("B")
    check("stream resumes after reauth + retry",
          ok and rows == ["b1", "b2", "b3", "b4", "b5", "b6"], rows)


SCENARIOS = {
    "normal": scenario_normal,
    "concurrent": scenario_concurrent,
    "late-401": scenario_late_401,
    "truncated": scenario_truncated,
    "conflict": scenario_conflict,
    "unknown": scenario_unknown,
    "reject": scenario_reject,
}


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    names = list(SCENARIOS) if which == "all" else [which]
    for name in names:
        SCENARIOS[name]()
    print(f"\n{len(FAILURES)} failure(s): {FAILURES}")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
