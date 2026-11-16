"""端到端演示（题面要求的反例与同链合法追加）。

运行：python3 probe.py
展示：
  1. 递归 root oracle 核对非 2 次幂检查点头；
  2. 替换第二片旧叶但保留第一叶 —— 新见证拒绝（旧原型曾接受）；
  3. 错误旧头（见证信任了别的 old_root）—— 拒绝；
  4. 正好幂次边界（63->64、64->65）—— 接受；
  5. 回退 / 同长度异 root / 错绑定 old_size / 截断 / 重复多余 / 非法 hash —— 拒绝；
  6. 见证失败不推进本地可信头，成功才原子推进；
  7. 空旧树特殊空证明；
  8. 合法追加的生成与见证接受形成同一条链。
"""
import json
import os

from logstore import LogStore
from merkle import root_hash, EMPTY_ROOT
from witness import Witness, WitnessError

RESULT = []


def record(title, ok, detail=''):
    RESULT.append({'case': title, 'accepted': bool(ok), 'detail': detail})
    print(('  [接受] ' if ok else '  [拒绝] ') + title + ((' — ' + detail) if detail else ''))


def reject_case(title, w, size, root, proof):
    before = (w.size, w.root)
    try:
        w.accept(size, root, proof)
    except WitnessError as exc:
        unchanged = (w.size, w.root) == before
        record(title, False, f'{exc}；失败后可信头不变={unchanged}')
        assert unchanged
        return
    raise AssertionError('应当被拒绝却接受了: ' + title)


def main():
    print('== 0. 递归 root oracle 核对非 2 次幂检查点头 ==')
    rows = [b'leaf-body-%02d' % i for i in range(130)]
    db_path = '/tmp/t466_demo_log.sqlite'
    if os.path.exists(db_path):
        os.remove(db_path)
    store = LogStore(db_path)
    store.append_batch(rows)
    for cp in (1, 2, 3, 7, 31, 63, 64, 65, 100, 127, 128, 129, 130):
        indexed = store.root_at(cp)
        oracle = root_hash(rows[:cp])
        assert indexed == oracle, cp
    print('  持久索引 root 与递归 oracle 在全部非 2 次幂检查点一致')

    print('== 1. 替换第二片旧叶、保留第一叶（旧原型的致命洞）==')
    old_size = 5
    trusted = root_hash(rows[:old_size]).hex()
    forged_rows = rows[:]
    forged_rows[1] = b'REPLACED-SECOND-LEAF'          # 第一片 rows[0] 保持不变
    forged = LogStore()
    forged.append_batch(forged_rows)
    w = Witness(old_size, trusted)
    reject_case('替换叶#2、保留叶#1 的伪造日志', w, *forged.head(),
                forged.consistency(old_size, len(forged_rows)))
    # 对照：同一份合法证明在诚实日志上被接受
    w_ok = Witness(old_size, trusted)
    honest_proof = store.consistency(old_size, 130)
    assert w_ok.accept(*store.head(), honest_proof)
    record('诚实日志的等价合法证明', True, '见证推进到 size=130')

    print('== 2. 错误旧头（见证信任了不匹配的 old_root）==')
    wrong_root = root_hash([b'unrelated'] * old_size).hex()
    w = Witness(old_size, wrong_root)
    reject_case('见证旧 root 与日志真实旧头不符', w, *store.head(),
                store.consistency(old_size, 130))

    print('== 3. 正好幂次边界 ==')
    for lo, hi in ((63, 64), (64, 65)):
        wb = Witness(lo, root_hash(rows[:lo]).hex())
        proof = store.consistency(lo, hi)
        ok = wb.accept(hi, root_hash(rows[:hi]).hex(), proof)
        record(f'幂次边界 {lo}->{hi}', ok, f'证明 {len(proof["hashes"])} 个 hash')

    print('== 4. 各类畸形/恶意证明 ==')
    base = Witness(old_size, trusted)
    good = store.consistency(old_size, 130)
    new_head = store.head()

    rb = Witness(old_size, trusted)
    reject_case('回退（new_size < trusted size）', rb, 4,
                root_hash(rows[:4]).hex(),
                {'old_size': old_size, 'new_size': 4, 'hashes': []})

    rs = Witness(old_size, trusted)
    reject_case('同长度异 root（equivocation）', rs, old_size,
                root_hash(forged_rows[:old_size]).hex(),
                {'old_size': old_size, 'new_size': old_size, 'hashes': []})

    rb2 = Witness(old_size, trusted)
    bad_bound = dict(good, old_size=old_size - 1)
    reject_case('错绑定 old_size', rb2, *new_head, bad_bound)

    rt = Witness(old_size, trusted)
    trunc = dict(good, hashes=good['hashes'][:-1])
    reject_case('截断的证明', rt, *new_head, trunc)

    re_ = Witness(old_size, trusted)
    extra = dict(good, hashes=good['hashes'] + [good['hashes'][0]])
    reject_case('多余证明节点（证明无法被恰好消费）', re_, *new_head, extra)

    ri = Witness(old_size, trusted)
    illegal = dict(good, hashes=good['hashes'][:-1] + ['zz' * 32])
    reject_case('非法 hash（非 hex / 非 32 字节）', ri, *new_head, illegal)

    print('== 5. 空旧树与同长度同 root 的特殊空证明 ==')
    w0 = Witness(0, EMPTY_ROOT.hex())
    empty_proof = store.consistency(0, 130)
    assert empty_proof['hashes'] == []
    assert w0.accept(*store.head(), empty_proof)
    record('空旧树 -> 首个检查点（空证明）', True, f'推进到 {w0.size}')
    ws = Witness(64, root_hash(rows[:64]).hex())
    same = store.consistency(64, 64)
    assert ws.accept(64, root_hash(rows[:64]).hex(), same)
    record('同长度同 root（空证明，幂等不回退）', True, f'size 仍为 {ws.size}')

    print('== 6. 合法追加的生成与见证接受形成同一条链 ==')
    log_db = '/tmp/t466_chain_demo.sqlite'
    wit_db = '/tmp/t466_chain_demo_wit.sqlite'
    for p in (log_db, wit_db):
        if os.path.exists(p):
            os.remove(p)
    chain = LogStore(log_db)
    wit = Witness(0, EMPTY_ROOT.hex(), wit_db)
    checkpoints = [1, 2, 3, 5, 8, 16, 31, 32, 33, 64, 100, 130]
    prev = 0
    for cp in checkpoints:
        chain.append_batch(rows[prev:cp])
        size, head = chain.head()
        assert head == root_hash(rows[:cp]).hex()
        p = chain.consistency(prev, cp)
        assert wit.accept(size, head, p)
        assert (wit.size, wit.root) == (cp, head)
        prev = cp
    record('沿 0->...->130 逐检查点同链推进', True,
           f'见证链尾=({wit.size}, {wit.root[:12]}...)')
    # 持久化见证重开后仍是同一链尾
    wit.close()
    wit2 = Witness(0, '', '/tmp/t466_chain_demo_wit.sqlite')
    assert (wit2.size, wit2.root) == (130, root_hash(rows).hex())
    record('见证状态持久化，重开后可信头不变', True, f'size={wit2.size}')
    wit2.close(); chain.close(); store.close()

    print('\n== 汇总 ==')
    print(json.dumps(RESULT, ensure_ascii=False, indent=2))
    rejected = [r for r in RESULT if not r['accepted']]
    accepted = [r for r in RESULT if r['accepted']]
    # 每个拒绝用例的 detail 都已断言“失败后可信头不变”
    assert rejected and accepted
    print(f'\n拒绝用例 {len(rejected)} 个，接受用例 {len(accepted)} 个；'
          f'所有伪造/畸形证明均未推进可信头。')


if __name__ == '__main__':
    main()
