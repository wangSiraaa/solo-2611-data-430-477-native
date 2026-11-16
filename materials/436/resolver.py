import hashlib
import struct

from cache import IO_CHUNK

MAX_OBJECT = 64 * 1024      # one object is at most 64KiB
MAX_DEPTH = 16             # delta chain depth at most 16
MAX_OBJECTS = 128          # at most 128 objects in an archive
OP_COPY = 0x01
OP_LITERAL = 0x02
OP_END = 0x00


class ResolveError(Exception):
    """Raised when an object cannot be verified and must not be delivered."""


def object_id(data):
    return hashlib.sha256(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


class Material:
    """Object bytes that may be resident in memory or spooled to a temp file.

    Two independent properties are tracked:

    * storage: ``data`` (resident, charged to the working-set ledger) or
      ``spool`` (a temp file, costing no memory; touched via 8KiB buffers);
    * ownership: an owned material is responsible for releasing its bytes; a
      borrowed material references verified cache bytes and frees nothing.

    Every ledger change happens here so callers cannot double-release.
    """

    def __init__(self, cache, data=None, spool=None, size=0, borrowed=False):
        self.cache = cache
        self.data = data
        self.spool = spool
        self.size = size if data is None else len(data)
        self.borrowed = borrowed
        self.closed = False
        # Borrowed cache bytes are never charged; spooled bytes carry no
        # resident charge; only owned resident bytes hit the ledger.
        self.charged = 0 if (borrowed or data is None) else len(data)
        if self.charged:
            cache.charge(self.charged)

    @classmethod
    def owned(cls, cache, data=None, spool=None, size=0):
        return cls(cache, data=data, spool=spool, size=size, borrowed=False)

    @classmethod
    def borrowed_cache(cls, cache, value):
        return cls(cache, data=value, borrowed=True)

    @property
    def resident(self):
        return self.data is not None

    def chunk(self, pos, size):
        if size == 0:
            return b''
        if self.data is not None:
            return self.data[pos:pos + size]
        return self.spool.read(pos, size)

    def load_all(self):
        if self.data is not None:
            return self.data
        return self.spool.read(0, self.size)

    def spill(self):
        """Move resident owned bytes into a temp file and drop their charge."""
        if self.closed or self.borrowed or self.data is None:
            return
        spool = self.cache.new_spool()
        spool.write(self.data)
        n = len(self.data)
        self.data = None
        self.spool = spool
        self.cache.release(n)
        self.charged = 0

    def take_resident(self):
        """Hand the resident bytes to the cache; removes them from the ledger."""
        if self.data is None:
            raise AssertionError('take_resident on non-resident material')
        value = self.data
        self.cache.release(self.charged)
        self.charged = 0
        self.data = None
        self.closed = True
        return value

    def take_spool(self):
        """Hand the spool to the cache; the ledger is untouched."""
        if self.spool is None:
            raise AssertionError('take_spool on resident material')
        spool = self.spool
        self.spool = None
        self.closed = True
        return spool

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.borrowed:
            self.data = None
            return
        if self.charged:
            self.cache.release(self.charged)
            self.charged = 0
            self.data = None
        elif self.spool is not None:
            self.cache.discard_spool(self.spool)
            self.spool = None


class Output:
    """Reconstruction target: resident bytearray, spilled to a spool on pressure.

    The output owns exactly one storage at a time and keeps its ledger charge
    in ``charged``; ``material()`` hands that storage over without recharging.
    """

    def __init__(self, resolver):
        self.resolver = resolver
        self.cache = resolver.cache
        self.buf = bytearray()
        self.spool = None
        self.size = 0
        self.charged = 0
        self.finished = False

    @property
    def resident(self):
        return self.spool is None

    def write(self, data):
        n = len(data)
        if n == 0:
            return
        if self.spool is None:
            # Evict other resident working materials first; only spill this
            # output itself when nothing else can make room.
            if not self.resolver.make_room(n):
                self.spill()
        if self.spool is not None:
            self.spool.write(data)
        else:
            self.buf.extend(data)
            self.cache.charge(n)
            self.charged += n
        self.size += n

    def spill(self):
        if self.spool is not None:
            return
        spool = self.cache.new_spool()
        spool.write(self.buf)
        self.buf = None
        self.spool = spool
        self.cache.release(self.charged)
        self.charged = 0

    def material(self):
        # Transfer the existing storage and charge; no double accounting.
        self.finished = True
        if self.spool is not None:
            spool = self.spool
            self.spool = None
            return Material.owned(self.cache, spool=spool, size=self.size)
        mat = Material.__new__(Material)
        mat.cache = self.cache
        mat.data = bytes(self.buf)
        mat.spool = None
        mat.size = len(mat.data)
        mat.borrowed = False
        mat.closed = False
        mat.charged = self.charged  # ledger charge moves with the bytes
        self.charged = 0
        self.buf = None
        return mat

    def discard(self):
        if self.finished:
            return
        self.finished = True
        if self.spool is not None:
            self.cache.discard_spool(self.spool)
            self.spool = None
        elif self.buf is not None:
            self.cache.release(self.charged)
            self.charged = 0
            self.buf = None


class Cursor:
    """Streaming reader over a resident or spooled payload.

    Reads beyond the payload raise ResolveError instead of silently
    truncating; spooled reads use a fixed 8KiB window (exempt from budget).
    """

    def __init__(self, material, label):
        self.mat = material
        self.label = label
        self.pos = 0
        self.end = material.size
        self.window = b''
        self.window_pos = 0

    def take(self, n):
        if n < 0 or self.pos + n > self.end:
            raise ResolveError('%s: truncated instruction stream' % self.label)
        if self.mat.data is not None:
            out = self.mat.data[self.pos:self.pos + n]
            self.pos += n
            return out
        out = bytearray()
        while len(out) < n:
            if self.window_pos >= len(self.window):
                want = min(IO_CHUNK, self.end - self.pos)
                self.window = self.mat.chunk(self.pos, want)
                self.window_pos = 0
                if not self.window:
                    raise ResolveError('%s: truncated instruction stream' % self.label)
            take = min(n - len(out), len(self.window) - self.window_pos)
            out += self.window[self.window_pos:self.window_pos + take]
            self.window_pos += take
            self.pos += take
        return bytes(out)

    def u32(self):
        return struct.unpack('>I', self.take(4))[0]

    def op(self):
        if self.pos >= self.end:
            raise ResolveError('%s: missing end marker' % self.label)
        return self.take(1)[0]

    def copy_literals(self, n, out):
        if self.pos + n > self.end:
            raise ResolveError('%s: truncated literal' % self.label)
        remaining = n
        while remaining:
            chunk_n = min(IO_CHUNK, remaining)
            out.write(self.take(chunk_n))
            remaining -= chunk_n

    def expect_end(self):
        if self.pos != self.end:
            raise ResolveError('%s: trailing bytes after end marker' % self.label)


class Resolver:
    def __init__(self, reader, cache):
        self.reader = reader
        self.cache = cache
        self.errors = []
        # Per-request bookkeeping.
        self._active = None
        self._spillables = None

    # ------------------------------------------------------------- public API
    def read(self, oid):
        if self.cache.contains(oid):
            # Count the hit through the same path as internal resolutions.
            return self.cache.get(oid)
        self._active = set()
        self._spillables = []
        material = None
        try:
            try:
                material = self._resolve(oid)
            except ResolveError as exc:
                self.errors.append({'oid': oid, 'error': str(exc)})
                raise
            return self._finalize(oid, material)
        finally:
            if material is not None:
                material.close()
            # A failed resolution must leave behind neither charged bytes nor
            # temp files; verified cache entries are untouched by failure.
            for holder in self._spillables:
                holder.close()
            self._active = None
            self._spillables = None

    def _finalize(self, oid, material):
        # A committed layer returns as a zero-cost borrow straight from cache.
        if material.borrowed:
            return material.data
        # Only an object larger than the whole budget reaches here: verify it
        # was identity-checked, hand its bytes back once without retaining.
        if material.spool is not None:
            return material.load_all()
        return material.take_resident()

    def _entry(self, oid):
        try:
            entry = self.reader.entry(oid)
        except KeyError:
            raise ResolveError('missing object %s' % oid)
        if not isinstance(self.reader.index, dict) or len(self.reader.index) > MAX_OBJECTS:
            raise ResolveError('archive holds more than %d objects' % MAX_OBJECTS)
        return entry

    def _resolve(self, oid, depth=0):
        if depth > MAX_DEPTH:
            raise ResolveError('delta chain deeper than %d' % MAX_DEPTH)
        cached = self.cache.get(oid)
        if cached is not None:
            return Material.borrowed_cache(self.cache, cached)
        if oid in self._active:
            raise ResolveError('delta cycle at %s' % oid)
        self._active.add(oid)
        try:
            entry = self._entry(oid)
            kind = entry.get('kind')
            declared_size = int(entry.get('size', -1))
            if declared_size < 0 or declared_size > MAX_OBJECT:
                raise ResolveError('%s: illegal declared size %r' % (oid, entry.get('size')))
            payload = self._read_payload(oid, entry)
            self._spillables.append(payload)
            if kind == 'full':
                self._verify(oid, entry, payload)
                return self._commit(oid, payload)
            if kind != 'delta':
                raise ResolveError('%s: unknown kind %r' % (oid, kind))
            base_oid = entry.get('base')
            if not base_oid:
                raise ResolveError('%s: delta without base' % oid)
            base = self._resolve(base_oid, depth + 1)
            out_material = None
            output = Output(self)
            try:
                self._apply_delta(oid, base, payload, output)
                out_material = output.material()
                self._verify(oid, entry, out_material)
                # Every layer is verified before it feeds the next copy; commit
                # it so a base shared by later requests is reused, not rebuilt.
                return self._commit(oid, out_material)
            except BaseException:
                if out_material is not None:
                    out_material.close()
                raise
            finally:
                output.discard()
                base.close()
                payload.close()
        except ResolveError:
            raise
        except Exception as exc:  # corrupt zlib, malformed structs, bad index ...
            raise ResolveError('%s: %s' % (oid, exc))
        finally:
            self._active.discard(oid)

    def _commit(self, oid, material):
        """Cache a verified object when it fits; always return a usable material.

        On success the cache owns a copy and the returned material is a
        zero-cost borrow; an object larger than the whole budget stays in its
        storage and the caller delivers it once without retention.
        """
        if material.size > self.cache.budget:
            return material
        if material.spool is not None:
            self.cache.adopt_spooled(oid, material)
        else:
            self.cache.adopt_resident(oid, material)
        return Material.borrowed_cache(self.cache, self.cache.get(oid))

    def _read_payload(self, oid, entry):
        # Memory planning for the decompressed payload:
        #  * full objects are terminal products -- verified then committed, so
        #    LRU cache entries may be evicted to make them resident; if even an
        #    empty cache cannot hold one, it is reconstructed in a spool and
        #    delivered directly (retention is impossible, delivery is not);
        #  * delta payloads sit beside their base chain, so they spill to a
        #    temp file (8KiB buffers only) rather than evicting a shared base.
        want = int(entry['size']) if entry.get('kind') == 'full' else MAX_OBJECT
        try:
            if entry.get('kind') == 'full' and want <= self.cache.budget:
                self.cache.evict_to_fit(int(entry['size']))
                data = self.reader.payload(entry)
                return Material.owned(self.cache, data=data)
            resident = self.cache.resident + self.cache.working + want <= self.cache.budget
            if resident:
                data = self.reader.payload(entry)
                return Material.owned(self.cache, data=data)
            spool = self.cache.new_spool()
            try:
                n = self.reader.decompress_to(entry, spool.f)
            except Exception:
                self.cache.discard_spool(spool)
                raise
            return Material.owned(self.cache, spool=spool, size=n)
        except ResolveError:
            raise
        except Exception as exc:
            raise ResolveError('%s: payload read failed: %s' % (oid, exc))

    def make_room(self, need):
        """Spill resident, owned materials until ``need`` more bytes fit."""
        if self.cache.resident + self.cache.working + need <= self.cache.budget:
            return True
        for holder in self._spillables:
            if not holder.resident or holder.borrowed:
                continue
            holder.spill()
            if self.cache.resident + self.cache.working + need <= self.cache.budget:
                return True
        return self.cache.resident + self.cache.working + need <= self.cache.budget

    def _apply_delta(self, oid, base, payload, out):
        label = 'delta %s' % oid[:12]
        cur = Cursor(payload, label)
        while True:
            op = cur.op()
            if op == OP_END:
                break
            if op == OP_COPY:
                start = cur.u32()
                n = cur.u32()
                if n == 0:
                    raise ResolveError('%s: zero-length copy' % label)
                if start > base.size or start + n < start or start + n > base.size:
                    raise ResolveError('%s: copy out of bounds [%d:+%d] base=%d'
                                       % (label, start, n, base.size))
                pos, remaining = start, n
                while remaining:
                    chunk_n = min(IO_CHUNK, remaining)
                    out.write(base.chunk(pos, chunk_n))
                    pos += chunk_n
                    remaining -= chunk_n
            elif op == OP_LITERAL:
                n = cur.u32()
                if n == 0:
                    raise ResolveError('%s: zero-length literal' % label)
                cur.copy_literals(n, out)
            else:
                raise ResolveError('%s: illegal opcode 0x%02x' % (label, op))
        cur.expect_end()

    def _verify(self, oid, entry, material):
        # Every layer is identity-checked before it can feed the next copy.
        declared = int(entry.get('size', -1))
        if material.size != declared:
            raise ResolveError('%s: size mismatch: got %d, declared %d'
                               % (oid, material.size, declared))
        h = hashlib.sha256()
        h.update(b'blob ' + str(material.size).encode() + b'\0')
        pos = 0
        while pos < material.size:
            n = min(IO_CHUNK, material.size - pos)
            h.update(material.chunk(pos, n))
            pos += n
        if h.hexdigest() != oid:
            raise ResolveError('%s: content id mismatch' % oid)
