"""穷举/随机正确性与攻击测试（不改变交付入口，仅作自测）。"""
import os
import random

from merkle import root_hash, verify_consistency, gen_consistency, verify_inclusion
from logstore import LogStore, LogError
from witness import Witness, WitnessError, _EMPTY_ROOT

random.seed(466)


def fresh_rows(n):
    return [os.urandom(1 + (i % 128)) for i in range(n)]


def test_roots_and_persistence():
    rows = fresh_rows(300)
    mem = LogStore()
    for b in rows:
        mem.append(b)
    for size in list(range(0, 20)) + [63, 64, 65, 127, 128, 255, 256, 300]:
        got = mem.root_at(size)
        want = root_hash(rows[:size])
        assert got == want, f'root mismatch at {size}'
    # 文件持久化 + 重开重建栈
    path = '/tmp/t466_log.sqlite'
    if os.path.exists(path):
        os.remove(path)
    disk = LogStore(path)
    disk.append_batch(rows[:100])
    disk.close()
    reopened = LogStore(path)
    assert reopened.size == 100
    assert reopened.root_at(100) == root_hash(rows[:100])
    reopened.append_batch(rows[100:300])
    assert reopened.root_at(300) == root_hash(rows[:300])
    # 历史检查点仍可查（不可变瓦片）
    for size in (1, 63, 64, 100, 127, 255, 300):
        assert reopened.root_at(size) == root_hash(rows[:size])
    reopened.close()
    print('test_roots_and_persistence OK')


def test_consistency_exhaustive():
    n = 257
    rows = fresh_rows(n)
    store = LogStore()
    store.append_batch(rows)
    for m in range(1, n):
        for nn in (m + 1, min(n, m * 2 + 1), n):
            if nn <= m:
                continue
            proof = store.consistency(m, nn)
            assert proof['old_size'] == m and proof['new_size'] == nn
            hashes = [bytes.fromhex(h) for h in proof['hashes']]
            old_root = root_hash(rows[:m])
            new_root = root_hash(rows[:nn])
            assert verify_consistency(m, nn, old_root, new_root, hashes), (m, nn)
            # 独立见证接受合法证明
            w = Witness(m, old_root.hex())
            assert w.accept(nn, new_root.hex(), proof)
            assert w.size == nn and w.root == new_root.hex()
    # 空旧树
    w0 = Witness(0, _EMPTY_ROOT.hex())
    assert w0.accept(n, root_hash(rows).hex(), store.consistency(0, n))
    # 同长度同 root 空证明
    w1 = Witness(64, root_hash(rows[:64]).hex())
    assert w1.accept(64, root_hash(rows[:64]).hex(), store.consistency(64, 64))
    print('test_consistency_exhaustive OK')


def test_merkle_verifier_standalone_matches():
    # 直接用纯算法 merkle 验证器与 witness 内嵌验证器交叉
    rows = fresh_rows(200)
    store = LogStore()
    store.append_batch(rows)
    from witness import _verify
    for m in (1, 2, 33, 63, 64, 100, 129):
        proof = store.consistency(m, 200)
        nodes = [bytes.fromhex(h) for h in proof['hashes']]
        assert _verify(m, 200, root_hash(rows[:m]), root_hash(rows), nodes)
        assert verify_consistency(m, 200, root_hash(rows[:m]), root_hash(rows), nodes)
    print('test_merkle_verifier_standalone_matches OK')


def test_attacks():
    rows = fresh_rows(10)
    store = LogStore()
    store.append_batch(rows)
    old_root = root_hash(rows[:5]).hex()
    w = Witness(5, old_root)

    def expect_reject(label, size, root, proof):
        before = (w.size, w.root)
        try:
            w.accept(size, root, proof)
        except WitnessError:
            assert (w.size, w.root) == before, f'state changed on reject: {label}'
            return
        raise AssertionError(f'attack accepted: {label}')

    # 1. 替换第二片旧叶、保留第一片（旧原型的致命洞）
    forged_rows = rows[:]
    forged_rows[1] = b'X' * 7
    forged = LogStore()
    forged.append_batch(forged_rows)
    forged_proof = forged.consistency(5, 10)
    expect_reject('replace leaf#2 keep leaf#1', *forged.head(), forged_proof)

    # 2. 错误旧头：见证信任了另一个 root，合法证明也必须被拒
    w_wrong = Witness(5, root_hash(fresh_rows(5)).hex())
    before = (w_wrong.size, w_wrong.root)
    try:
        w_wrong.accept(*store.head(), store.consistency(5, 10))
        raise AssertionError('wrong trusted old root accepted')
    except WitnessError:
        assert (w_wrong.size, w_wrong.root) == before
    # 同 size 但见证 root 与真实旧头不符
    w2 = Witness(5, root_hash(forged_rows[:5]).hex())
    try:
        w2.accept(*store.head(), store.consistency(5, 10))
        raise AssertionError('wrong old root accepted')
    except WitnessError:
        assert (w2.size, w2.root) == (5, root_hash(forged_rows[:5]).hex())

    # 3. 回退
    expect_reject('rollback size', 4, root_hash(rows[:4]).hex(),
                  {'old_size': 5, 'new_size': 4, 'hashes': []})

    # 4. 同长度异 root
    expect_reject('same size diff root', 5, root_hash(forged_rows[:5]).hex(),
                  {'old_size': 5, 'new_size': 5, 'hashes': []})

    # 5. 错绑定 old_size
    bad = dict(store.consistency(5, 10)); bad['old_size'] = 4
    expect_reject('bad bound old_size', *store.head(), bad)

    # 6. 截断
    p = store.consistency(5, 10)
    trunc = dict(p, hashes=p['hashes'][:-1])
    expect_reject('truncated proof', *store.head(), trunc)

    # 7. 多余/重复节点
    extra = dict(p, hashes=p['hashes'] + [p['hashes'][0]])
    expect_reject('extra proof node', *store.head(), extra)
    dup = dict(p, hashes=p['hashes'] + p['hashes'])
    expect_reject('duplicated proof tail', *store.head(), dup)

    # 8. 非法 hash
    illegal = dict(p, hashes=p['hashes'][:-1] + ['zz' * 32])
    expect_reject('illegal hex hash', *store.head(), illegal)
    short = dict(p, hashes=p['hashes'][:-1] + ['ab' * 16])
    expect_reject('short hash', *store.head(), short)

    # 9. 用另一棵树的合法证明嫁接
    other = LogStore(); other.append_batch(fresh_rows(10))
    expect_reject('foreign proof graft', *store.head(), other.consistency(5, 10))

    # 10. 空旧树却附带节点
    w0 = Witness(0, _EMPTY_ROOT.hex())
    try:
        w0.accept(10, root_hash(rows).hex(),
                  {'old_size': 0, 'new_size': 10, 'hashes': [p['hashes'][0]]})
        raise AssertionError('empty-tree proof with nodes accepted')
    except WitnessError:
        assert (w0.size, w0.root) == (0, _EMPTY_ROOT.hex())
    print('test_attacks OK')


def test_power_of_two_boundary():
    rows = fresh_rows(130)
    store = LogStore(); store.append_batch(rows)
    for boundary in (1, 2, 4, 8, 16, 32, 64, 128):
        proof = store.consistency(boundary, boundary + 1)
        w = Witness(boundary, root_hash(rows[:boundary]).hex())
        assert w.accept(boundary + 1, root_hash(rows[:boundary + 1]).hex(), proof)
        # 反向：从 power-1 扩到正好 power 边界
        if boundary > 1:
            proof2 = store.consistency(boundary - 1, boundary)
            w2 = Witness(boundary - 1, root_hash(rows[:boundary - 1]).hex())
            assert w2.accept(boundary, root_hash(rows[:boundary]).hex(), proof2)
    print('test_power_of_two_boundary OK')


def test_inclusion_still_works():
    rows = fresh_rows(50)
    store = LogStore(); store.append_batch(rows)
    for i in range(50):
        m = store.inclusion(i, 50)
        path = [(s, bytes.fromhex(h)) for s, h in m['path']]
        assert verify_inclusion(rows[i], path, root_hash(rows))
    print('test_inclusion_still_works OK')


def test_body_limits_and_cap():
    s = LogStore()
    try:
        s.append(b'')
        raise AssertionError('empty body accepted')
    except LogError:
        pass
    try:
        s.append(b'x' * 129)
        raise AssertionError('129-byte body accepted')
    except LogError:
        pass
    s.append_batch([b'x'] * 4096)
    assert s.size == 4096
    try:
        s.append(b'y')
        raise AssertionError('cap exceeded')
    except LogError:
        pass
    print('test_body_limits_and_cap OK')


def test_witness_durability():
    path = '/tmp/t466_wit.sqlite'
    if os.path.exists(path):
        os.remove(path)
    rows = fresh_rows(8)
    store = LogStore(); store.append_batch(rows)
    w = Witness(2, root_hash(rows[:2]).hex(), path)
    w.accept(4, root_hash(rows[:4]).hex(), store.consistency(2, 4))
    w.close()
    w2 = Witness(0, '', path)  # 忽略入参，从磁盘恢复
    assert (w2.size, w2.root) == (4, root_hash(rows[:4]).hex())
    # 失败不推进，且磁盘头不变
    forged = rows[:]; forged[0] = b'Q'
    f = LogStore(); f.append_batch(forged)
    try:
        w2.accept(*f.head(), f.consistency(4, 8))
        raise AssertionError('forged accepted')
    except WitnessError:
        pass
    assert (w2.size, w2.root) == (4, root_hash(rows[:4]).hex())
    w2.close()
    w3 = Witness(0, '', path)
    assert (w3.size, w3.root) == (4, root_hash(rows[:4]).hex())
    w3.close()
    print('test_witness_durability OK')


def test_reference_oracle_crosscheck():
    """用完全独立的参考实现（直接在叶子上递归，不经持久索引）交叉核对。"""
    import hashlib

    def H(x):
        return hashlib.sha256(x).digest()

    def lh(b):
        return H(b'\x00' + b)

    def nh(a, b):
        return H(b'\x01' + a + b)

    def mth(rows):
        n = len(rows)
        if n == 0:
            return H(b'')
        if n == 1:
            return lh(rows[0])
        k = 1 << ((n - 1).bit_length() - 1)
        return nh(mth(rows[:k]), mth(rows[k:]))

    def ref_gen(m, rows, n):
        out = []

        def sub(mm, nn, st):
            if mm == nn:
                out.append(mth(rows[st:st + nn]))
                return
            k = 1 << ((nn - 1).bit_length() - 1)
            if mm <= k:
                sub(mm, k, st)
                out.append(mth(rows[st + k:st + nn]))
            else:
                out.append(mth(rows[st:st + k]))
                sub(mm - k, nn - k, st + k)

        sub(m, n, 0)
        return out

    n = 200
    rows = fresh_rows(n)
    store = LogStore(); store.append_batch(rows)
    for m in (1, 2, 3, 7, 8, 31, 32, 33, 63, 64, 100, 127, 128, 199):
        # 证明哈希与参考实现逐字节一致
        store.reset_reads()
        proof = store.consistency(m, n)['hashes']
        ref = [x.hex() for x in ref_gen(m, rows, n)]
        assert proof == ref, (m, 'proof differs from reference oracle')
        # 参考 root 与持久 root 一致
        assert store.root_at(m) == mth(rows[:m])
        assert store.root_at(n) == mth(rows)
    print('test_reference_oracle_crosscheck OK')


def test_tamper_each_old_leaf():
    """逐一替换每一片旧叶，任何一片被改都必须被见证拒绝（针对原首叶漏洞）。"""
    n, m = 12, 6
    rows = fresh_rows(n)
    good = LogStore(); good.append_batch(rows)
    trusted_root = root_hash(rows[:m]).hex()
    for victim in range(m):
        bad_rows = rows[:]
        bad_rows[victim] = b'TAMPERED-%d' % victim
        bad = LogStore(); bad.append_batch(bad_rows)
        w = Witness(m, trusted_root)
        try:
            w.accept(*bad.head(), bad.consistency(m, n))
            raise AssertionError(f'tamper of old leaf {victim} accepted')
        except WitnessError:
            assert (w.size, w.root) == (m, trusted_root)
    # 替换新增叶（索引 >= m）不影响旧前缀绑定，但会改变 new_root，证明随之失败
    bad_rows = rows[:]; bad_rows[m] = b'NEW-TAMPER'
    bad = LogStore(); bad.append_batch(bad_rows)
    w = Witness(m, trusted_root)
    try:
        w.accept(n, root_hash(bad_rows).hex(), good.consistency(m, n))
        raise AssertionError('new-root mismatch accepted')
    except WitnessError:
        pass
    print('test_tamper_each_old_leaf OK')


def test_repeated_hash_values_allowed():
    """不同结构位置出现相同哈希值（内容相同的叶）是合法证明，必须接受；
    而真正多余的节点仍由“证明恰好消费完”结构性拒绝。"""
    rows = [b'A', b'A', b'B', b'C']
    s = LogStore(); s.append_batch(rows)
    p = s.consistency(1, 4)
    assert len(set(p['hashes'])) < len(p['hashes'])     # 确有重复值
    w = Witness(1, root_hash(rows[:1]).hex())
    assert w.accept(*s.head(), p)
    # 追加一个多余节点 -> 结构消费不完，拒绝
    extra = dict(p, hashes=p['hashes'] + [p['hashes'][-1]])
    w2 = Witness(1, root_hash(rows[:1]).hex())
    try:
        w2.accept(*s.head(), extra)
        raise AssertionError('structurally extra node accepted')
    except WitnessError:
        assert (w2.size, w2.root) == (1, root_hash(rows[:1]).hex())
    print('test_repeated_hash_values_allowed OK')


def test_chain_linkage():
    """合法追加形成同一条链：见证沿 size 0..N 逐检查点推进，头与日志严格一致。"""
    rows = fresh_rows(64)
    path = '/tmp/t466_chain.sqlite'
    wit_path = '/tmp/t466_chain_wit.sqlite'
    for p in (path, wit_path):
        if os.path.exists(p):
            os.remove(p)
    store = LogStore(path)
    w = Witness(0, _EMPTY_ROOT.hex(), wit_path)
    checkpoints = [1, 2, 3, 5, 8, 13, 21, 32, 48, 64]
    prev = 0
    for cp in checkpoints:
        store.append_batch(rows[prev:cp])
        size, head = store.head()
        assert (size, head) == (cp, root_hash(rows[:cp]).hex())
        proof = store.consistency(prev, cp)
        assert w.accept(size, head, proof)
        assert (w.size, w.root) == (cp, head)
        prev = cp
    # 重开见证，状态仍停在链尾
    w.close()
    w2 = Witness(0, '', '/tmp/t466_chain_wit.sqlite')
    assert (w2.size, w2.root) == (64, root_hash(rows).hex())
    w2.close(); store.close()
    print('test_chain_linkage OK')


if __name__ == '__main__':
    test_roots_and_persistence()
    test_consistency_exhaustive()
    test_merkle_verifier_standalone_matches()
    test_reference_oracle_crosscheck()
    test_attacks()
    test_tamper_each_old_leaf()
    test_power_of_two_boundary()
    test_inclusion_still_works()
    test_body_limits_and_cap()
    test_witness_durability()
    test_repeated_hash_values_allowed()
    test_chain_linkage()
    print('ALL TESTS PASSED')
