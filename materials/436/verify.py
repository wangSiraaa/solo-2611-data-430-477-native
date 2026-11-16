"""Independent verification for the content-addressed pack resolver.

Builds real binary pack fixtures (zlib payloads + index.json) in a temp
directory, then exercises: full objects, cross-pack multi-layer deltas,
shared-base reuse, corrupt bases, copy-boundary errors, delta cycles,
truncated/illegal/trailing instructions, missing bases, size/id mismatch,
failure isolation, deep chains, and over-budget sequences with spills.
"""
import hashlib
import json
import struct
import tempfile
import zlib
from pathlib import Path

from cache import Cache
from pack_reader import PackReader
from resolver import MAX_DEPTH, ResolveError, Resolver, object_id

BUDGET = 256 * 1024
FAILS = []


def check(name, cond, detail=''):
    status = 'ok  ' if cond else 'FAIL'
    print('%s %s %s' % (status, name, detail))
    if not cond:
        FAILS.append(name)


def expect_error(name, fn, needle):
    try:
        fn()
    except ResolveError as exc:
        check(name, needle in str(exc), str(exc))
        return
    except Exception as exc:
        check(name, False, 'wrong exception: %r' % exc)
        return
    check(name, False, 'no error raised')


class Fixture:
    def __init__(self, root):
        self.root = Path(root)
        self.packs = {}
        self.index = {}

    def add_raw(self, oid, payload, kind, size, base, pack):
        raw = zlib.compress(payload)
        buf = self.packs.setdefault(pack, bytearray())
        self.index[oid] = {'pack': pack, 'offset': len(buf), 'length': len(raw),
                           'kind': kind, 'size': size, 'base': base}
        buf.extend(raw)

    def add_corrupt_pack_bytes(self, oid, raw, kind, size, base, pack):
        """Write bytes verbatim into the pack (no zlib) to simulate corruption."""
        buf = self.packs.setdefault(pack, bytearray())
        self.index[oid] = {'pack': pack, 'offset': len(buf), 'length': len(raw),
                           'kind': kind, 'size': size, 'base': base}
        buf.extend(raw)

    def add_full(self, data, pack='a.pack'):
        oid = object_id(data)
        self.add_raw(oid, data, 'full', len(data), None, pack)
        return oid

    def add_delta(self, data, base_oid, ops, pack='a.pack'):
        oid = object_id(data)
        self.add_raw(oid, ops, 'delta', len(data), base_oid, pack)
        return oid

    def write(self):
        self.root.mkdir(exist_ok=True)
        for name, buf in self.packs.items():
            (self.root / name).write_bytes(buf)
        (self.root / 'index.json').write_text(json.dumps(self.index))

    def reader_cache_resolver(self, budget=BUDGET):
        r = PackReader(self.root)
        c = Cache(budget)
        return r, c, Resolver(r, c)


def copy_op(start, n):
    return b'\x01' + struct.pack('>II', start, n)


def lit_op(data):
    return b'\x02' + struct.pack('>I', len(data)) + data


def fake_oid(tag):
    """Deterministic, collision-free placeholder oid for malformed entries."""
    return hashlib.sha256(b'fake-oid:' + tag.encode()).hexdigest()


def main():
    tmp = tempfile.mkdtemp(prefix='packverify-')

    # ------------------------------------------------------------ happy paths
    fx = Fixture(Path(tmp) / 'good')
    base = b'hello world' * 200                      # 2200 bytes
    base_oid = fx.add_full(base, 'a.pack')
    local_ops = copy_op(0, len(base)) + lit_op(b'!') + b'\x00'
    local_oid = fx.add_delta(base + b'!', base_oid, local_ops, 'a.pack')
    cross_ops = copy_op(0, len(base)) + lit_op(b'?') + b'\x00'
    cross_oid = fx.add_delta(base + b'?', base_oid, cross_ops, 'b.pack')
    chain_ops = copy_op(0, len(base) + 1) + lit_op(b'#') + b'\x00'
    chain_oid = fx.add_delta(base + b'?#', cross_oid, chain_ops, 'b.pack')
    # second child of the same base for the shared-base reuse check
    sib_ops = copy_op(0, len(base)) + lit_op(b'~') + b'\x00'
    sib_oid = fx.add_delta(base + b'~', base_oid, sib_ops, 'b.pack')
    fx.write()
    r, c, res = fx.reader_cache_resolver()

    got = res.read(base_oid)
    check('full plaintext', got == base and object_id(got) == base_oid)
    got = res.read(local_oid)
    check('in-pack delta', got == base + b'!' and object_id(got) == local_oid)
    got = res.read(cross_oid)
    check('cross-pack delta', got == base + b'?' and object_id(got) == cross_oid)
    got = res.read(chain_oid)
    check('multi-layer cross-pack chain', got == base + b'?#' and object_id(got) == chain_oid)
    base_reads = [x for x in r.reads if x['offset'] == fx.index[base_oid]['offset']
                  and x['pack'] == 'a.pack']
    check('base pack read once despite 3 dependents', len(base_reads) == 1,
          'reads=%d' % len(base_reads))
    check('verified base reused from cache', c.contains(base_oid) and c.hits >= 3)

    # shared base: sibling delta must not re-decompress the base
    reads_before = len(r.reads)
    got = res.read(sib_oid)
    check('shared-base sibling content', got == base + b'~')
    new_reads = r.reads[reads_before:]
    check('shared base not re-read', all(x['pack'] != 'a.pack' for x in new_reads),
          json.dumps(new_reads))

    # valid copy boundary: start+n == base.size is legal
    edge_ops = copy_op(len(base) - 1, 1) + b'\x00'
    edge_oid = fx.add_delta(base[-1:], base_oid, edge_ops, 'b.pack')
    fx.write()
    r2, c2, res2 = fx.reader_cache_resolver()
    check('copy at exact end boundary', res2.read(edge_oid) == base[-1:])

    # ------------------------------------------------------------- failure mix
    bad = Fixture(Path(tmp) / 'bad')
    good_oid = bad.add_full(b'keep me, verified and resident')
    # a valid base that malformed deltas can actually reach before failing
    okbase = b'Z' * 64
    okbase_oid = bad.add_full(okbase, 'a.pack')
    # corrupt base: same length, different bytes, index still names the real oid
    real = b'A' * 64
    corrupt = b'B' * 64
    claimed_oid = object_id(real)
    bad.add_raw(claimed_oid, corrupt, 'full', len(real), None, 'a.pack')
    child_ops = copy_op(0, len(real)) + b'\x00'
    child_oid = fake_oid('corrupt-child')  # distinct from the corrupt base
    bad.add_raw(child_oid, child_ops, 'delta', len(real), claimed_oid, 'a.pack')

    # copy out of bounds (one past end) -- valid base, bad instruction
    ob_ops = copy_op(len(okbase), 1) + b'\x00'
    ob_oid = fake_oid('oob')
    bad.add_raw(ob_oid, ob_ops, 'delta', 1, okbase_oid, 'a.pack')
    # truncated instruction: copy header cut short
    trunc_oid = fake_oid('trunc')
    bad.add_raw(trunc_oid, b'\x01\x00\x02', 'delta', 0, okbase_oid, 'a.pack')
    # illegal opcode
    ill_oid = fake_oid('illegal')
    bad.add_raw(ill_oid, b'\x09\x00', 'delta', 0, okbase_oid, 'a.pack')
    # trailing bytes after end marker
    trail_oid = fake_oid('trail')
    bad.add_raw(trail_oid, b'\x00\x02', 'delta', 0, okbase_oid, 'a.pack')
    # missing end marker
    noend_oid = fake_oid('noend')
    bad.add_raw(noend_oid, lit_op(b'x'), 'delta', 1, okbase_oid, 'a.pack')
    # declared size mismatch (index says 5, delta builds 1 byte)
    sm_oid = fake_oid('size')
    bad.add_raw(sm_oid, lit_op(b'x') + b'\x00', 'delta', 5, okbase_oid, 'a.pack')
    # literal truncated: declares 4 literal bytes, supplies 2
    lt_oid = fake_oid('lit-trunc')
    bad.add_raw(lt_oid, b'\x02' + struct.pack('>I', 4) + b'ab', 'delta', 4, okbase_oid, 'a.pack')
    # missing base reference
    ghost = fake_oid('ghost-base')
    mb_oid = fake_oid('missing-base')
    bad.add_raw(mb_oid, b'\x00', 'delta', 0, ghost, 'a.pack')
    # cycle: c1 -> c2 -> c1
    c1 = fake_oid('cycle-1')
    c2 = fake_oid('cycle-2')
    bad.add_raw(c1, b'\x00', 'delta', 1, c2, 'a.pack')
    bad.add_raw(c2, b'\x00', 'delta', 1, c1, 'a.pack')
    # corrupt intermediate layer: index oid claims different bytes than built
    mid_real = b'layer-correct'
    mid_built = b'layer-wrong!!'
    mid_oid = object_id(mid_real)
    bad.add_raw(mid_oid, mid_built, 'full', len(mid_built), None, 'b.pack')
    leaf_ops = copy_op(0, len(mid_built)) + b'\x00'
    leaf_oid = hashlib.sha256(b'blob ' + str(len(mid_built)).encode()
                              + b'\0' + mid_built).hexdigest()
    bad.add_raw(leaf_oid, leaf_ops, 'delta', len(mid_built), mid_oid, 'b.pack')
    bad.write()
    rb, cb, resb = bad.reader_cache_resolver()

    check('good object delivered before failures', resb.read(good_oid) == b'keep me, verified and resident')
    expect_error('corrupt base rejected (id mismatch)', lambda: resb.read(claimed_oid), 'content id mismatch')
    expect_error('child of corrupt base rejected', lambda: resb.read(child_oid), 'content id mismatch')
    expect_error('copy one-past-end rejected', lambda: resb.read(ob_oid), 'out of bounds')
    expect_error('truncated instruction rejected', lambda: resb.read(trunc_oid), 'truncated')
    expect_error('illegal opcode rejected', lambda: resb.read(ill_oid), 'illegal opcode')
    expect_error('trailing bytes rejected', lambda: resb.read(trail_oid), 'trailing')
    expect_error('missing end marker rejected', lambda: resb.read(noend_oid), 'end marker')
    expect_error('declared size mismatch rejected', lambda: resb.read(sm_oid), 'size mismatch')
    expect_error('truncated literal rejected', lambda: resb.read(lt_oid), 'truncated literal')
    expect_error('missing base rejected', lambda: resb.read(mb_oid), 'missing object')
    expect_error('delta cycle rejected', lambda: resb.read(c1), 'cycle')
    expect_error('intermediate layer identity enforced', lambda: resb.read(leaf_oid), 'content id mismatch')

    # failure isolation: verified object must survive other objects failing
    check('verified result kept after failures', cb.contains(good_oid))
    check('verified content still correct', resb.read(good_oid) == b'keep me, verified and resident')
    check('failed object never cached', not cb.contains(claimed_oid) and not cb.contains(c1))
    check('no working-set leakage after failures', cb.working == 0, 'working=%d' % cb.working)

    # corrupted zlib stream: undecodable bytes written verbatim into the pack
    cz = Fixture(Path(tmp) / 'cz')
    z_oid = fake_oid('bad-zlib')
    cz.add_corrupt_pack_bytes(z_oid, b'\xff\xff\xff\xff\xff\xff', 'full', 15, None, 'a.pack')
    cz.write()
    _, _, resz = cz.reader_cache_resolver()
    expect_error('corrupt zlib rejected', lambda: resz.read(z_oid), 'payload read failed')

    # ------------------------------------------------------- deep chain + spill
    deep = Fixture(Path(tmp) / 'deep')
    layer0 = b'Z' * 20000
    o0 = deep.add_full(layer0, 'a.pack')
    prev, prev_data = o0, layer0
    chain_oids = [o0]
    for i in range(1, MAX_DEPTH + 1):
        data = prev_data + bytes([65 + (i % 26)])
        ops = copy_op(0, len(prev_data)) + lit_op(data[-1:]) + b'\x00'
        prev = deep.add_delta(data, prev, ops, 'a.pack')
        prev_data = data
        chain_oids.append(prev)
    deep.write()
    # Tight budget: two 20KiB layers alone exceed it, so reconstruction must
    # spill earlier layers into temp files rather than refuse the deep chain.
    rd, cd, resd = deep.reader_cache_resolver(budget=32 * 1024)
    got = resd.read(chain_oids[-1])
    check('16-level chain reconstructs', got == prev_data and object_id(got) == chain_oids[-1],
          'len=%d' % len(got))
    check('deep chain peak within budget', cd.peak <= 32 * 1024, 'peak=%d' % cd.peak)
    check('deep chain spilled to temp files', cd.temp_written > 0,
          'temp_written=%d' % cd.temp_written)
    check('spooled bytes read back', cd.temp_read > 0, 'temp_read=%d' % cd.temp_read)

    # chain beyond depth limit
    over = Fixture(Path(tmp) / 'overdepth')
    p = over.add_full(b'0', 'a.pack')
    last_data = b'0'
    for i in range(MAX_DEPTH + 2):
        last_data = last_data + b'x'
        p = over.add_delta(last_data, p, lit_op(b'x') + b'\x00', 'a.pack')
    over.write()
    _, _, reso = over.reader_cache_resolver()
    expect_error('depth limit enforced', lambda: reso.read(p), 'deeper than')

    # ------------------------------------------- total objects larger than budget
    big = Fixture(Path(tmp) / 'big')
    blob = b'Q' * 20000
    oids = [big.add_full(blob + bytes([i]), 'b.pack') for i in range(20)]  # 400KiB total
    big.write()
    rb2, cb2, resb2 = big.reader_cache_resolver(budget=32 * 1024)
    ok = True
    for i, oid in enumerate(oids):
        try:
            v = resb2.read(oid)
            if v != blob + bytes([i]) or object_id(v) != oid:
                ok = False
        except Exception as exc:
            ok = False
            print('   delivery %d raised %r' % (i, exc))
    check('over-budget sequence all delivered correctly', ok)
    check('over-budget peak really bounded', cb2.peak <= 32 * 1024, 'peak=%d' % cb2.peak)
    check('over-budget used spill or LRU eviction',
          cb2.temp_written > 0 or cb2.evictions > 0,
          'spill=%d evict=%d' % (cb2.temp_written, cb2.evictions))
    check('over-budget compressed bytes accounted',
          rb2.compressed_bytes_total == sum(x['length'] for x in rb2.index.values()))

    # --------------------------- object larger than the whole cache budget
    osfx = Fixture(Path(tmp) / 'oversize')
    big_one = b'K' * 50000
    big_oid = osfx.add_full(big_one, 'a.pack')
    osfx.write()
    ros, cos, resos = osfx.reader_cache_resolver(budget=40000)
    v = resos.read(big_oid)
    check('oversize object delivered verified once', v == big_one and object_id(v) == big_oid)
    check('oversize object not retained', not cos.contains(big_oid))
    check('oversize delivery leaves clean ledger', cos.working == 0 and cos.peak <= 40000,
          'peak=%d working=%d' % (cos.peak, cos.working))
    v2 = resos.read(big_oid)
    check('oversize object re-derivable', v2 == big_one)

    # ------------------------------------------------------- accounting sanity
    r3, c3, res3 = fx.reader_cache_resolver()
    res3.read(cross_oid)
    reads_after_first = len(r3.reads)
    res3.read(cross_oid)
    check('second identical read is a cache hit',
          len(r3.reads) == reads_after_first and c3.contains(cross_oid))
    check('peak never exceeds budget anywhere', c.peak <= BUDGET and cb.peak <= BUDGET
          and cd.peak <= BUDGET and c3.peak <= BUDGET)

    print()
    if FAILS:
        print('FAILURES: %s' % ', '.join(FAILS))
        raise SystemExit(1)
    print('all checks passed')


if __name__ == '__main__':
    main()
