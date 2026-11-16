"""独立透明日志见证（Witness）。

自包含：不导入日志包，哈希原语与一致性验证全部在本文件内重新实现，
删除日志实现后见证仍能仅凭本地可信头 + 新头 + 证明完成验证。

见证只持有对数状态 (size, root)。所有可拒绝的输入都在写入前检查，
验证通过后才在单个事务内原子推进；任何失败都保留原头不变。
"""
import hashlib
import sqlite3

_HASH_LEN = 32
_EMPTY_ROOT = hashlib.sha256(b'').digest()


def _h(data):
    return hashlib.sha256(data).digest()


def _node(left, right):
    return _h(b'\x01' + left + right)


def _split_k(n):
    return 1 << ((n - 1).bit_length() - 1)


class WitnessError(ValueError):
    pass


def _as_hash(token):
    """非法 hash（非 32 字节 / 非 hex）直接拒绝。"""
    if not isinstance(token, str) or len(token) != _HASH_LEN * 2:
        raise WitnessError('illegal hash: must be 32-byte hex')
    try:
        raw = bytes.fromhex(token)
    except ValueError:
        raise WitnessError('illegal hash: not hexadecimal')
    if raw.hex() != token.lower():
        raise WitnessError('illegal hash: non-canonical hex')
    return raw


def _verify(m, n, old_root, new_root, hashes):
    """与生成端同构的递归一致性验证（见证内独立实现，无任何日志包依赖）。

    成功 True；结构非法、截断、多余哈希均 False。仅凭 (m,old_root)、
    (n,new_root) 与证明哈希独立重算出旧/新两个 root 并比对。
    """
    if not (0 < m < n) or not isinstance(hashes, list):
        return False
    it = iter(hashes)
    try:
        def rec(m_, n_):
            if m_ == n_:
                h = next(it)                    # 整段新旧共有
                return h, h
            k = _split_k(n_)
            if m_ <= k:
                ol, nl = rec(m_, k)             # 旧树只在左半
                rr = next(it)                   # 右半为新增，不并入旧根
                return ol, _node(nl, rr)
            else:
                ll = next(it)                   # 左完美子树新旧共有
                or_, nr = rec(m_ - k, n_ - k)
                return _node(ll, or_), _node(ll, nr)

        old_calc, new_calc = rec(m, n)
        if next(it, None) is not None:          # 多余/重复证明节点
            return False
    except StopIteration:                        # 截断的证明
        return False
    return old_calc == old_root and new_calc == new_root


class Witness:
    def __init__(self, size, root, path=None):
        if not isinstance(size, int) or size < 0:
            raise WitnessError('bad size')
        self._path = path
        self.size = size
        self.root = root
        self.db = None
        self.accepted = []
        if path is not None:
            self.db = sqlite3.connect(path)
            self.db.execute(
                'create table if not exists state('
                'id integer primary key check(id=1), size integer not null, '
                'root text not null)')
            self.db.execute(
                'create table if not exists history('
                'size integer primary key, root text not null)')
            self.db.commit()
            row = self.db.execute('select size,root from state where id=1').fetchone()
            if row is not None:
                self.size, self.root = row
            else:
                self._persist(self.size, self.root, insert=True)

    # ------------------------------------------------------------- 持久化
    def _persist(self, size, root, insert=False):
        if self.db is None:
            return
        with self.db:
            if insert:
                self.db.execute(
                    'insert into state(id,size,root) values(1,?,?)', (size, root))
            else:
                self.db.execute(
                    'update state set size=?, root=? where id=1', (size, root))
            self.db.execute(
                'insert or replace into history(size,root) values(?,?)', (size, root))

    def _snapshot(self):
        return self.size, self.root

    # --------------------------------------------------------------- 验证
    def verify(self, new_size, new_root, proof):
        """纯校验：通过返回 True，否则抛 WitnessError；不改变任何状态。"""
        old_size, old_root = self.size, self.root
        if not isinstance(new_size, int) or not isinstance(new_root, str):
            raise WitnessError('malformed new head')
        if not isinstance(proof, dict):
            raise WitnessError('malformed proof')

        # 回退检查（size 与 root 都不允许倒退）
        if new_size < old_size:
            raise WitnessError('rollback: new size smaller than trusted size')
        if new_size == old_size and new_root != old_root:
            raise WitnessError('equivocation: same size but different root')

        bound = proof.get('old_size')
        if bound != old_size:
            raise WitnessError(f'proof binds old_size={bound}, trusted={old_size}')
        if proof.get('new_size') != new_size:
            raise WitnessError('proof new_size does not match new head')

        tokens = proof.get('hashes')
        if not isinstance(tokens, list):
            raise WitnessError('proof hashes must be a list')
        # 注意：不同结构位置允许出现相同哈希值（如两片内容相同的叶），不能按值去重；
        # 真正多余/重复的节点由 _verify 末尾“证明必须恰好消费完”结构性地拒绝。
        nodes = [_as_hash(t) for t in tokens]

        if old_size == 0:
            # 空旧树：必须使用特殊空证明；新头须结构合法
            if nodes:
                raise WitnessError('empty old tree requires empty proof')
            if new_size == 0 and new_root != _EMPTY_ROOT.hex():
                raise WitnessError('empty tree root mismatch')
            return True

        if new_size == old_size:
            # 同长度同 root 的特殊空证明
            if new_root != old_root:
                raise WitnessError('same-size head has different root')
            if nodes:
                raise WitnessError('same-size proof must be empty')
            return True

        if len(nodes) > 64:
            raise WitnessError('proof too large')
        try:
            old_raw = bytes.fromhex(old_root)
            new_raw = bytes.fromhex(new_root)
        except ValueError:
            raise WitnessError('trusted/new head root is not hex')
        if len(old_raw) != _HASH_LEN or len(new_raw) != _HASH_LEN:
            raise WitnessError('illegal head hash length')

        if not _verify(old_size, new_size, old_raw, new_raw, nodes):
            raise WitnessError(
                'consistency check failed: old prefix not proven unchanged')
        return True

    def accept(self, new_size, new_root, proof):
        """验证通过才原子推进；失败保留原头并抛 WitnessError。"""
        self.verify(new_size, new_root, proof)          # 任何失败在此抛出，不写状态
        # 到这里所有检查都已通过，单事务推进本地可信头
        self.size, self.root = new_size, new_root
        self._persist(new_size, new_root)
        self.accepted.append((new_size, new_root))
        return True

    def close(self):
        if self.db is not None:
            self.db.close()
