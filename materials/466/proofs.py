"""证明门面：保留旧入口名 inclusion / verify_inclusion / consistency。

真正的纯算法在 merkle.py，持久查询在 logstore.LogStore。旧原型的
consistency 只证明首叶存在（故意错误）；本版本返回完整 RFC6962 等价
一致性证明，绑定整个旧前缀。
"""
from merkle import (
    leaf_hash, node_hash, root_hash,
    gen_inclusion, verify_inclusion, gen_consistency, verify_consistency,
)

# 向后兼容的裸名
leaf = leaf_hash
parent = node_hash


def inclusion(rows, index):
    """旧风格：给定叶序列的 membership 证明（递归 root oracle 版本）。"""
    def mth(start, count):
        return root_hash(rows[start:start + count])
    return gen_inclusion(index, len(rows), mth)


def verify_inclusion_hex(body, path, root_hex):
    """path 中哈希可为 bytes 或 hex 字符串；root_hex 为 hex。"""
    norm = []
    for side, h in path:
        norm.append((side, h if isinstance(h, bytes) else bytes.fromhex(h)))
    root = bytes.fromhex(root_hex) if isinstance(root_hex, str) else root_hex
    return verify_inclusion(body, norm, root)


def consistency(store, old_size, new_size=None):
    """基于持久子树索引生成一致性证明（不重新扫描叶子）。"""
    return store.consistency(old_size, new_size)
