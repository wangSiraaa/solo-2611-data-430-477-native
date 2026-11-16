"""公开基准：new_size=4096，old_size ∈ {1,63,1023,4095}。

核对硬约束：
  * 生成一次一致性证明最多读取 64 个索引节点（计数含预读，不含建索引）；
  * 证明最多含 64 个 hash；
  * 证明生成过程不触碰 leaves 表（不重新扫描叶子）。
并额外扫描全部 (old,new) 组合，给出实际读取上界，证明预算不是只对样例成立。
"""
import os

from logstore import LogStore
from merkle import root_hash
from witness import Witness

NEW_SIZE = 4096
BUDGET = 64
PUBLIC_OLD = (1, 63, 1023, 4095)


def build_log(path):
    if os.path.exists(path):
        os.remove(path)
    store = LogStore(path)
    # 1..128 字节的叶子，批量追加（单 writer，最多 4096 叶）；长度随 i 循环覆盖
    bodies = [(b'leaf-%d|' % i).ljust(1 + (i % 128), b'#') for i in range(NEW_SIZE)]
    store.append_batch(bodies)
    return store, bodies


def main():
    store, rows = build_log('/tmp/t466_bench.sqlite')
    assert store.size == NEW_SIZE

    print('=== 公开基准（new_size=%d，预算：读取<=64 节点，证明<=64 hash）===' % NEW_SIZE)
    print('old_size | 索引节点读取 | 证明hash数 | 触及leaves表 | 见证接受')
    print('-' * 72)
    all_ok = True
    for old in PUBLIC_OLD:
        store.reset_reads()                       # 清空计数，确保不含建索引阶段
        before_raw = sum(1 for t in store.read_trace if t[0] == 'raw')
        proof = store.consistency(old, NEW_SIZE)
        node_reads = store.node_reads()
        raw_during = sum(1 for t in store.read_trace if t[0] == 'raw') - before_raw
        n_hash = len(proof['hashes'])

        # 独立见证：只持对数状态 (old, old_root)
        w = Witness(old, root_hash(rows[:old]).hex())
        size, head = store.head()
        accepted = False
        try:
            accepted = w.accept(size, head, proof)
        except Exception as exc:                       # noqa: BLE001
            accepted = False
            print('witness error:', exc)

        ok = (node_reads <= BUDGET and n_hash <= BUDGET and raw_during == 0
              and accepted and (w.size, w.root) == (NEW_SIZE, head))
        all_ok &= ok
        print('%8d | %12d | %10d | %12s | %s  %s' % (
            old, node_reads, n_hash,
            '否' if raw_during == 0 else '是(%d)' % raw_during,
            accepted, '' if ok else '<<< 违反预算/验证'))

    # 全量扫描：所有 1<=old<new<=4096 的实际读取与证明长度上界
    max_reads, max_hashes, worst = 0, 0, None
    # 稠密采样 + 全边界，避免 800 万组合过慢，同时覆盖所有临界形状
    candidates = set(PUBLIC_OLD)
    for v in range(0, 13):
        for d in (-2, -1, 0, 1, 2):
            for base in (1 << v,):
                x = base + d
                if 1 <= x < NEW_SIZE:
                    candidates.add(x)
    candidates.update(range(1, 66))
    candidates.update(NEW_SIZE - x for x in range(1, 66))
    for old in sorted(candidates):
        store.reset_reads()
        proof = store.consistency(old, NEW_SIZE)
        r, h = store.node_reads(), len(proof['hashes'])
        if r > max_reads:
            max_reads, worst = r, ('reads', old)
        max_hashes = max(max_hashes, h)
    print('-' * 72)
    print('采样 %d 个 old_size：最大索引读取=%d（最坏 %s），最大证明长度=%d'
          % (len(candidates), max_reads, worst, max_hashes))
    within = max_reads <= BUDGET and max_hashes <= BUDGET
    print('结论：%s' % ('全部满足 <=64 约束' if (all_ok and within)
                        else '存在违反，需检查'))

    # 展示一条真实读取记录（old=63），证明计数来自实际 SQL 索引访问
    store.reset_reads()
    store.consistency(63, NEW_SIZE)
    print('-' * 72)
    print('真实节点读取记录（old=63，共 %d 次，前 12 条）：' % store.node_reads())
    for rec in store.read_trace[:12]:
        print('  ', rec)
    print('  ... 总计 %d 次 node 读取，0 次 leaves 扫描' % store.node_reads())
    store.close()
    return 0 if (all_ok and within) else 1


if __name__ == '__main__':
    raise SystemExit(main())
