"""RFC6962 域分离哈希原语、递归 root oracle 与一致性证明纯算法。

本模块只含无 I/O 的纯协议逻辑，供持久日志 (logstore) 与独立见证 (witness)
共同使用；见证所需的全部语义也在其内部再实现一份，因此即便删除日志包，
见证仍可独立验证。

规则（题面）：
  叶节点   = SHA256(0x00 || body)
  内部节点 = SHA256(0x01 || left || right)
  空树 root= SHA256(b'')
  非 2 次幂树：按小于叶数的最大 2 次幂切成左右子树（与 RFC6962 MTH 相同）。
"""
import hashlib

LEAF_PREFIX = b'\x00'
NODE_PREFIX = b'\x01'
EMPTY_ROOT = hashlib.sha256(b'').digest()
HASH_LEN = 32
MAX_LEAVES = 4096
MIN_BODY, MAX_BODY = 1, 128


def H(data):
    return hashlib.sha256(data).digest()


def leaf_hash(body):
    return H(LEAF_PREFIX + body)


# 兼容旧原型命名
leaf = leaf_hash


def node_hash(left, right):
    return H(NODE_PREFIX + left + right)


parent = node_hash


def split_k(n):
    """小于 n 的最大 2 次幂（RFC6962 切分点 k）。仅 n>=2 时调用。"""
    return 1 << ((n - 1).bit_length() - 1)


def root_hash(leaves):
    """递归 root oracle：给定叶序列计算 MTH。用于独立核对持久索引。"""
    n = len(leaves)
    if n == 0:
        return EMPTY_ROOT
    if n == 1:
        return leaf_hash(leaves[0])
    k = split_k(n)
    return node_hash(root_hash(leaves[:k]), root_hash(leaves[k:]))


def is_power_of_two(n):
    return n > 0 and (n & (n - 1)) == 0


# ---------------------------------------------------------------------------
# 一致性证明（RFC6962 §2.1.2 等价的递归 SUBPROOF 结构）。
# 证明只是一串有序哈希；验证端仅凭 (m, old_root)、(n, new_root) 与该串即可
# 同时重算出 old_root 与 new_root，从而绑定整个旧前缀而非某一片叶。
# ---------------------------------------------------------------------------
def gen_consistency(m, n, mth):
    """生成 old_size=m -> new_size=n 的有序哈希列表 (0<m<n)。

    mth(start, count) 需返回区间 [start, start+count) 的子树根。
    与 verify_consistency 严格同构（互为逆过程），无需 m=1 特判：第 0 片叶
    也进入证明，验证端重算出的旧根必须恰好等于已信任 old_root。
    """
    assert 0 < m < n
    out = []

    def subproof(m_, n_, start):
        if m_ == n_:
            out.append(mth(start, n_))          # 该整段新旧共有
            return
        k = split_k(n_)
        if m_ <= k:
            subproof(m_, k, start)              # 旧边界落在左半，右半为新增
            out.append(mth(start + k, n_ - k))
        else:
            out.append(mth(start, k))           # 左完美子树新旧共有
            subproof(m_ - k, n_ - k, start + k)

    subproof(m, n, 0)
    return out


def verify_consistency(m, n, old_root, new_root, hashes):
    """RFC6962 等价的一致性验证（与 gen_consistency 同构的递归对偶）。

    成功返回 True；结构不合法、截断或多余哈希均返回 False。
    验证端只用 (m, old_root)、(n, new_root) 与证明哈希，独立重算出两个 root：
    old_calc 必须等于已信任的 old_root，new_calc 必须等于新头 root，
    从而在密码学上确认整个旧前缀原序不变（而非仅某一片叶存在）。
    """
    if not (0 < m < n) or not isinstance(hashes, list):
        return False
    it = iter(hashes)
    try:
        def rec(m_, n_):
            """返回 (该区间旧子树根, 该区间新子树根)，按证明顺序消费哈希。"""
            if m_ == n_:
                h = next(it)                    # 整段新旧共有
                return h, h
            k = split_k(n_)
            if m_ <= k:
                ol, nl = rec(m_, k)             # 旧树只在左半
                rr = next(it)                   # 右半为新增，不并入旧根
                return ol, node_hash(nl, rr)
            else:
                ll = next(it)                   # 左完美子树新旧共有
                or_, nr = rec(m_ - k, n_ - k)
                return node_hash(ll, or_), node_hash(ll, nr)

        old_calc, new_calc = rec(m, n)
        leftover = next(it, None)               # 多余/重复节点
    except StopIteration:                        # 截断的证明
        return False
    if leftover is not None:
        return False
    return old_calc == old_root and new_calc == new_root


def gen_inclusion(index, n, mth):
    """给定 mth(start,count) 访问器生成 membership 证明（有序 (side,hash)）。"""
    out = []

    def sub(idx, start, count):
        if count == 1:
            return
        k = split_k(count)
        if idx < k:
            sub(idx, start, k)
            out.append(('R', mth(start + k, count - k)))   # 兄弟在右
        else:
            sub(idx - k, start + k, count - k)              # 局部索引减去 k
            out.append(('L', mth(start, k)))                # 兄弟在左

    sub(index, 0, n)
    return out


def verify_inclusion(body, path, root):
    value = leaf_hash(body)
    for side, node in path:
        value = node_hash(value, node) if side == 'R' else node_hash(node, value)
    return value == root
