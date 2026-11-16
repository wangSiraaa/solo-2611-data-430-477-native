# 463 — SQLite 跨租户会话归还协议

`Session` 持有一条真实 `sqlite3.Connection`；`Pool` 在容量受限的物理连接上发放
`Lease`；`Query` 在租约上运行可在行边界暂停的查询。本实现把「归还」做成一个有顺序、
可验证、可淘汰的协议，并让取消/归还与查询线程可以安全地来自不同线程。

## 文件

| 文件 | 作用 |
| --- | --- |
| `session.py` | 连接级状态：默认 PRAGMA 快照、绑定/解除租户、光标与工作线程登记、清理与**清理后自检**、可观察快照。 |
| `pool.py` | 池与 `Lease`：容量/租约数上限、归还协议的收束与等待、失败淘汰、活动连接与租约统计。 |
| `executor.py` | `Query`：启动准入与租约关闭原子化、取消只作用于自己的租约、`return_connection` 先中止再归还。 |
| `probe.py` | 原泄漏复现 + 暂停归还 / 清理失败淘汰 / 迟到取消交错 / 容量的端到端记录。 |
| `test_session_protocol.py` | 26 个确定性测试（真实连接、真实线程、事件同步）。 |

## 运行

```bash
cd materials/463
python3 probe.py            # JSON 行为记录（校验失败返回非零）
python3 probe.py --human    # 缩进可读版本
python3 test_session_protocol.py -v
```

## 归还协议

`Pool.release` / `Query.return_connection`：

1. **关闭租约**（`Lease.begin_return`，持租约锁）：置 `closed`，`Query.start` 与
   `cancel` 从此被拒；置 abort 标志并 `sqlite3.interrupt()`。
2. **收束旧操作**：`wait_for_workers` 等待该租约登记的工作线程退出，**不持池锁**。
   暂停在行边界的 worker 通过 abort 标志立即解阻。
3. **清理**（`Session.cleanup`）：回滚事务 → 临时开放写（`query_only`）→ 关闭残留光标
   → 删除 TEMP 触发器/视图/索引/表 → 删除 `main`（私有 `:memory:`）对象 → DETACH
   所有非 `main`/`temp` 库（含 `tenant`）→ 恢复建连时读到的默认 PRAGMA。
4. **自检**：无事务、无 TEMP/main 对象、库列表仅 `main,temp`、全部 PRAGMA 等于默认值。
5. **回收或淘汰**：自检通过才放回空闲队列；任何异常、超时或自检失败都**关闭该连接**，
   绝不放回半清理连接。

### 线程与容量

- `begin_return` 与 `Query.start` 的「登记 + 起线程」互斥，归还不会在 worker 起跑前
  清掉连接，worker 也不会在归还后启动。
- `Lease.cancel` 在租约关闭或 `session.current_lease` 已不是自己时返回 `False`；
  因此**旧 `Query.cancel` 在新租约开始后是空操作**，不会打断新工作。
- 容量统计包含：空闲、活动、正在创建（`_pending`）、正在关闭（`_retiring`）的连接。
  重建/淘汰期间不释放容量名额，活动连接数 `<= capacity`，不遗留工作线程。
- 连接创建与关闭都在池锁之外完成；持池锁等待的另一租约不会阻塞无关空闲连接出借。

### 会话默认值

建连时应用并回读，回读值即基线：`foreign_keys`、`query_only`、`temp_store`、
`busy_timeout`、`recursive_triggers`、`defer_foreign_keys`。新租约只看到自己的
`tenant` 库、声明的默认 PRAGMA、无 TEMP 残留、无未提交事务。

### 可观察性

`Pool.snapshot()`：容量、总/空闲/活动/创建中/关闭中的连接、活动租约与名额、
`stats`（created/reused/evicted/cleanups/failed_cleanups/aborted_workers/…）以及
每个租约的状态。`Lease.snapshot()` / `Query.status()` / `Session.snapshot()`
提供更细的运行时视图。

## 实际行为记录

`python3 probe.py --human`（本机 Python 3.11.2 / SQLite 3.40.1）的关键字段：

```json
{
  "leak_repro": {"normal_tenant": "b", "leaked_temp_table": false},
  "paused_return": {
    "before": {"paused_done": false, "worker_alive": true},
    "after": {
      "return_seconds": 0.01, "worker_stopped": true,
      "next_tenant": "b", "next_temp_objects": [],
      "next_databases": ["main", "temp", "tenant"],
      "next_in_transaction": false, "next_foreign_keys": 1
    }
  },
  "cleanup_error_eviction": {
    "lease_state": "evicted", "evicted": true,
    "old_session_alive": false, "total_connections": 1,
    "rebuilt_tenant": "b", "failed_cleanups": 1
  },
  "late_cancel": {
    "stale_cancel_delivered": false, "new_query_survived": true,
    "new_query_state": "done", "new_query_error": null, "new_query_rows": ["b"]
  },
  "capacity": {
    "capacity": 2, "peak_total_connections": 2,
    "total_connections": 2, "idle_connections": 2,
    "active_leases": 0, "lease_slots_in_use": 0, "max_leases": 8
  }
}
```

测试：`Ran 26 tests ... OK`（连续 10 次无波动）。另做过随机并发压测（8 个租约
并发、随机 cancel/return），结束后无遗留 `query-*` 线程、空闲连接全部洁净、
`total_connections <= capacity`、零错误。

## 与原始材料的关系

`Pool.acquire(tenant, path, timeout)`、`Pool.release(lease)`、`Query.start/cancel/
return_connection` 入口保留并向后兼容（`Session.reset()` 仍然只做回滚）。
`probe.py` 的首个字段仍是原来的泄漏复现，现在输出 `"leaked_temp_table": false`。
