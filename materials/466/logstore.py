"""持久透明日志：叶子表 + 可查询的持久化完美子树索引（SQLite）。

索引模型（RFC6962 风格的完美“瓦片”）：
  nodes(level, idx, hash) 表示覆盖叶子区间 [idx, idx+2^level) 的完美子树根，
  idx 统一为叶子起点（level=0 行即叶子 id）。追加叶子时按二进制进位合并，
  所有产生过的瓦片都持久保留（不可变，历史检查点仍可查询），因此一致性证明
  与历史 root 都只查索引，绝不重新扫描 leaves 表。

每次索引读取都记录到 self.read_trace，基准用它核对“最多 64 个索引节点”。
"""
import sqlite3

from merkle import (
    MAX_BODY, MAX_LEAVES, MIN_BODY, EMPTY_ROOT,
    leaf_hash, node_hash, split_k,
    gen_consistency, gen_inclusion, is_power_of_two,
)


class LogError(ValueError):
    pass


class LogStore:
    def __init__(self, path=':memory:'):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.execute(
            'create table if not exists meta(key text primary key, value integer)')
        self.db.execute(
            'create table if not exists leaves(id integer primary key, body blob not null)')
        self.db.execute(
            'create table if not exists nodes('
            'level integer not null, idx integer not null, hash blob not null, '
            'primary key(level, idx)) without rowid')
        self.db.commit()
        # 二进制进位栈：[(level, idx, hash), ...]，按叶子区间从左到右入栈
        self._stack = []
        self._rebuild_stack()
        # 每次证明/查询周期开始前可 reset_reads() 清空，保证计数不含建索引
        self.read_trace = []
        self._cache = {}

    # ------------------------------------------------------------------ 基础
    @property
    def size(self):
        row = self.db.execute("select value from meta where key='size'").fetchone()
        return row[0] if row else 0

    def _canonical_tiles(self, size):
        """size 的 RFC6962 规范切分：每段都是持久化的完美瓦片，
        返回 [(level, leaf_start), ...]，按叶子位置从左到右。"""
        tiles, start, remain = [], 0, size
        while remain:
            if is_power_of_two(remain):
                # 完美段（含 remain==1）：整段就是一个持久瓦片
                tiles.append((remain.bit_length() - 1, start))
                break
            k = 1 << ((remain - 1).bit_length() - 1)   # 小于 remain 的最大 2 次幂
            tiles.append((k.bit_length() - 1, start))
            start += k
            remain -= k
        return tiles

    def _rebuild_stack(self):
        """重开库后由持久瓦片重建进位栈（当前 size 的规范切分即栈内容）。"""
        tile_hash = {(lv, idx): h for lv, idx, h in
                     self.db.execute('select level, idx, hash from nodes').fetchall()}
        self._stack = [(lv, idx, tile_hash[(lv, idx)])
                       for lv, idx in self._canonical_tiles(self.size)]

    def reset_reads(self):
        self.read_trace = []
        self._cache = {}

    def _read_node(self, level, idx):
        """一次真实索引节点读取（含计数与缓存；缓存命中不产生新读取）。"""
        key = ('node', level, idx)
        if key in self._cache:
            return self._cache[key]
        row = self.db.execute(
            'select hash from nodes where level=? and idx=?', (level, idx)).fetchone()
        self.read_trace.append(('node', level, idx))
        if row is None:
            raise LogError(f'missing index node level={level} idx={idx}')
        self._cache[key] = row[0]
        return row[0]

    def node_reads(self):
        return sum(1 for t in self.read_trace if t[0] == 'node')

    # ------------------------------------------------------------------ 写入
    def append(self, body):
        return self.append_batch([body])

    def append_batch(self, bodies):
        bodies = list(bodies)
        for body in bodies:
            if not isinstance(body, (bytes, bytearray)) or not (MIN_BODY <= len(body) <= MAX_BODY):
                raise LogError(f'leaf body must be {MIN_BODY}..{MAX_BODY} bytes')
        if self.size + len(bodies) > MAX_LEAVES:
            raise LogError(f'log holds at most {MAX_LEAVES} leaves')
        with self.db:
            cur_size = self.size
            for body in bodies:
                idx = cur_size
                h = leaf_hash(bytes(body))
                self.db.execute(
                    'insert into leaves(id,body) values(?,?)', (idx, bytes(body)))
                self.db.execute(
                    'insert into nodes(level,idx,hash) values(0,?,?)', (idx, h))
                self._stack.append((0, idx, h))
                cur_size += 1
                # 二进制进位：栈顶两个同级完美子树合并
                while len(self._stack) >= 2 and self._stack[-1][0] == self._stack[-2][0]:
                    lv, rix, rh = self._stack.pop()
                    _, lix, lh = self._stack.pop()
                    parent = node_hash(lh, rh)
                    self._stack.append((lv + 1, lix, parent))
                    self.db.execute(
                        'insert or replace into nodes(level,idx,hash) values(?,?,?)',
                        (lv + 1, lix, parent))
            self.db.execute(
                "insert or replace into meta(key,value) values('size',?)", (cur_size,))
        return cur_size

    # ------------------------------------------------------------- 只读查询
    def leaves(self, size=None):
        rows = [r[0] for r in self.db.execute(
            'select body from leaves order by id').fetchall()]
        self.read_trace.append(('raw', len(rows)))
        return rows if size is None else rows[:size]

    def _subtree_root(self, start, count):
        """任意区间 [start,start+count) 的根，经持久瓦片折叠，带 memo。"""
        key = ('sub', start, count)
        if key in self._cache:
            return self._cache[key]
        if is_power_of_two(count):
            level = count.bit_length() - 1
            root = self._read_node(level, start)  # idx 统一为叶子起点
        else:
            k = split_k(count)
            root = node_hash(self._subtree_root(start, k),
                             self._subtree_root(start + k, count - k))
        self._cache[key] = root
        return root

    def root_at(self, size):
        """历史检查点 root：只折叠持久规范瓦片，读取数 = 规范瓦片数（O(log size)）。

        规范瓦片按叶子位置从左到右（如 size7 -> [T4,T2,T1]）。RFC6962 的
        不规则树是 P(T4, P(T2,T1))，故从最右瓦片开始做右折叠：
        acc = parent(左侧瓦片, acc)。
        """
        if size < 0 or size > self.size:
            raise LogError('size out of range')
        if size == 0:
            return EMPTY_ROOT
        # 规范瓦片按叶子位置从左到右（如 size7 -> [T4,T2,T1]）。RFC6962 的
        # 不规则树是 P(T4, P(T2,T1))，故从最右瓦片开始做右折叠：
        # acc = parent(左侧瓦片, acc)。
        root = None
        for level, start in reversed(self._canonical_tiles(size)):
            h = self._read_node(level, start)
            root = h if root is None else node_hash(h, root)
        return root

    def head(self, size=None):
        if size is None:
            size = self.size
        return size, self.root_at(size).hex()

    def consistency(self, old_size, new_size=None):
        """生成 old_size -> new_size 一致性证明；只走索引，不扫描叶子。"""
        if new_size is None:
            new_size = self.size
        if not (0 <= old_size <= new_size <= self.size):
            raise LogError('require 0 <= old_size <= new_size <= current size')
        if old_size == 0 or old_size == new_size:
            # 空旧树 / 同长度：特殊空证明（同长度时见证另校验 root 相等）
            return {'old_size': old_size, 'new_size': new_size, 'hashes': []}
        hashes = [h.hex() for h in
                  gen_consistency(old_size, new_size, self._subtree_root)]
        return {'old_size': old_size, 'new_size': new_size, 'hashes': hashes}

    def inclusion(self, index, size=None):
        """单叶 membership 证明（同样基于持久索引）。"""
        if size is None:
            size = self.size
        if not (0 <= index < size <= self.size):
            raise LogError('index out of range')
        path = [(side, h.hex()) for side, h in
                gen_inclusion(index, size, self._subtree_root)]
        return {'index': index, 'size': size, 'path': path}

    def close(self):
        self.db.close()
