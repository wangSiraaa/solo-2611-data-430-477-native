# 多域原子准入调度器（materials/467）

一个采集请求同时占用 **全局 / 端点 / 租户** 三类限流额度。本目录把原来“只检查
首个域”的调度器改成多域原子准入调度器，并把“发送扣费”和“并发名额回收”彻底
分开。

## 设计

### `limits.py` — `Bucket` + `QuotaLedger`

* `Bucket(capacity, per_ms, max_active, initial_tokens)`
  * `capacity` 为整数 `1..20`；`per_ms` 是正有理数（`int/float/str/Fraction`），
    内部统一转成 `Fraction`，因此虚拟时钟下的扣费是**精确**的（如 `1/3`）。
  * token 用有理数保存；`max_active` 是并发名额；`frozen_until` 是冻结截止时刻。
  * 所有查询（`blockers` / `available` / `next_available_time` / `project`）都是
    **纯投影**，不会推进桶的内部时钟，避免一次“能不能发”的探询改变后续单调时钟
    语义。
  * 只有 `occupy()` 会推进时钟并扣除 token；只有 `release()` 会归还并发名额。
* `QuotaLedger`
  * `admit(id, domains, cost, now)`：先对**所有**域做一次检查；只有全部满足才
    在一个循环里逐域 `occupy`。任一域不可用则整体失败，**不留下任何部分占额**
    （不做“先扣 A 再等 B”）。
  * 每次成功发送生成一张不可变 `Ticket`，记录 `domains / cost / admitted_at /
    state / outcome / settled_at`，可用 `ticket(id)`、`tickets()` 逐票据查询。
  * `settle(id, now, outcome)`：**只释放并发名额，不退还 token**，且只生效一次。
    重复结算记录 `settle-duplicate` 事件并原样返回首次终态，因此发送后的
    取消/响应竞争不会双释放。
  * `throttle(domain, until)`：按域冻结，互不影响。
  * `check(...)` 是不扣费的可行性探询；`reconcile()` 把“活动票据的域计数”与
    “桶的 `active` 计数”对账。
  * 兼容旧接口：`begin()` 返回是否整体准入，`finish()` 结算但不退 token。

### `dispatcher.py` — `Dispatcher`

* `submit(Request)` 只入队；`tick(now)` 推进调用方提供的单调整数毫秒时钟并尝试
  发送所有可行请求（返回本次启动数）。
* **多域原子准入**：请求携带的域名必须互不重复、整数 `cost <=` 每个所需桶的
  容量（提交时校验），任一域 token/并发/冻结不满足就整单等待。
* **公平与防饥饿**（同一优先级内按提交序）：
  * 一个暂时不可行的请求会把它的**全部域**加入本轮预约集；后续请求若与预约集
    相交则跳过，因此不会抢走它正在积累的 token（预约）；
  * 预约集**跨优先级生效**：低优先级请求不能在同一轮消耗高优先级阻塞请求所需
    的额度；
  * 与预约集**完全不相交**的请求照常放行，不受阻塞请求影响；
  * 保证成立的条件：`cost <=` 各域容量、`max_active >= 1`、已发送请求最终都会
    结算、调用方持续推进单调时钟。此时被阻塞请求的域持续补充而其重叠后继被
    压住，有限毫秒内必然可行。更高优先级持续占满时低优先级会等待——这正是
    优先级语义。
* **429 冻结**：响应的 `scope`（字符串或列表；缺省退化为该请求的全部域）按
  `Retry-After` 毫秒冻结对应域，其他域不受影响；**已发送请求不会被撤回**，只是
  其票据照常结算。
* **取消**：
  * 排队中取消：无票据，**不扣 token**，状态改为 `cancelled`；
  * 发送后取消：与正常响应竞争，`QuotaLedger` 单次结算保证只有一个终态、并发
    名额只释放一次；晚到的响应记为 `late-response` 不产生第二次结算。
* **异步完成归属**：回调按 `request_id` 归属到 `active` 表；`tick` 持有可重入锁，
  同步完成的传输（在 `start()` 内直接回调）可以安全重入，不会递归重排。
* **可查询**：
  * `metering(now)`：限流计量视图（各域 token/active/冻结 + 票据 + 对账）；
  * `lifecycle()`：请求生命周期视图（pending 顺序、active、每个请求状态与事件）；
  * `status(id)` / `why_not(id)`：某个排队请求当前被哪个域、因何（`tokens` /
    `concurrency` / `frozen`）挡住，以及预计可发时刻——用于判断是否提前发送。

### `transport.py`

* `ControlledTransport`：回调可控，测试可指定任意完成顺序；`finish` 对同一
  请求只投递一次。
* `ThreadTransport`：每个请求一个真实线程跑 `fn(request)`，完成异步回调；
  异常被收敛为 `{'error': ...}`；`cancel` 与线程内完成竞争时也只投递一次。
  回调在传输锁之外调用，允许回调内重入调度器。

## 运行

```bash
cd materials/467
python3 probe.py                 # 打印各场景运行记录（JSON）
python3 -m unittest -v test_scheduler   # 单元测试
```

`probe_output.txt` 是一次完整运行记录，覆盖：

1. 多域共享：租户无 token 时全局也不被扣（无部分占额），补齐后整单发送；
2. 虚拟时钟精确扣费与“结束后不退款”；
3. 冻结作用域：仅 `tenant` 冻结，`other` 域请求继续；`frozen_until` 恰好解冻；
4. 已发送请求遇 429 不被撤回；
5. 取消前（排队，不扣 token）与取消后（与响应竞争，单次结算、不双释放）；
6. 公平：阻塞的大请求靠预约先于其重叠小请求发送，完全不相交的请求立即发送；
7. `ThreadTransport` 下的异步完成、回调重入、取消/响应竞争与对账一致；
8. 计量与生命周期分别可查询。

## 关键不变量

* 发送时一次性扣除 token，结束后永不退还；
* 并发名额在**唯一终态**释放，取消与响应竞争不会双释放；
* 任一域不可用时不留下部分占额（不存在跨域长期占位）；
* 冻结只作用于 429 指定的 scope 域，已发送请求不被撤回；
* `ledger.reconcile()` 恒为 `consistent: true`，调度与计量可对账。
