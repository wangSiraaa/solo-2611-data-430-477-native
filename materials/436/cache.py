import tempfile

# Fixed I/O buffer size: the fixed read/write buffer of an I/O stream is part
# of the streaming machinery and does not count against the decoding working
# set (only object payload/output/cached bytes do).
IO_CHUNK = 8192


class Spool:
    """Materialized object bytes kept in a temporary file.

    File bytes are not part of the in-memory working set; the 8KiB streaming
    buffers used to touch them are likewise exempt.  Read/write byte totals
    are recorded so callers can report real spill traffic.
    """

    def __init__(self, tmpdir):
        self.f = tempfile.TemporaryFile(dir=tmpdir)
        self.size = 0
        self.bytes_written = 0
        self.bytes_read = 0

    def write(self, data):
        mv = memoryview(data)
        self.f.seek(0, 2)
        while mv:
            n = self.f.write(mv[:IO_CHUNK])
            mv = mv[n:]
        self.size += len(data)
        self.bytes_written += len(data)

    def read(self, pos, size):
        self.f.seek(pos)
        chunks = []
        remaining = size
        while remaining:
            buf = self.f.read(min(IO_CHUNK, remaining))
            if not buf:
                break
            chunks.append(buf)
            remaining -= len(buf)
        out = b''.join(chunks)
        self.bytes_read += len(out)
        return out

    def close(self):
        self.f.close()


class Cache:
    """Bounded LRU cache plus the decoding working-set ledger.

    Two kinds of bytes live under ``budget``:

    * resident verified objects (``data``), managed as an LRU;
    * transient bytes of objects still being reconstructed or borrowed
      decompressed payloads (``working``).

    Cache entries are only evicted when a *verified* object is committed, so a
    failed resolution can never delete an already verified correct result.
    Spooled materials and streaming 8KiB I/O buffers do not consume budget.
    """

    def __init__(self, budget, tmpdir=None):
        self.budget = budget
        self.tmpdir = tmpdir
        self.data = {}          # oid -> bytes
        self.order = []         # LRU order, oldest first
        self.resident = 0
        self.working = 0
        self.peak = 0           # overall working-set peak (resident + working)
        self.resident_peak = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.temp_written = 0
        self.temp_read = 0
        self.spools = []

    # ------------------------------------------------------------------ ledger
    def _snapshot(self):
        total = self.resident + self.working
        if total > self.budget:
            raise AssertionError('working set %d exceeds budget %d' % (total, self.budget))
        self.peak = max(self.peak, total)
        self.resident_peak = max(self.resident_peak, self.resident)

    def charge(self, n):
        self.working += n
        self._snapshot()

    def release(self, n):
        self.working -= n
        if self.working < 0:
            raise AssertionError('negative working set')
        self._snapshot()

    # ------------------------------------------------------------------ spools
    def new_spool(self):
        spool = Spool(self.tmpdir)
        self.spools.append(spool)
        return spool

    def discard_spool(self, spool):
        self.temp_written += spool.bytes_written
        self.temp_read += spool.bytes_read
        spool.close()
        try:
            self.spools.remove(spool)
        except ValueError:
            pass

    # ------------------------------------------------------------------- cache
    def get(self, oid):
        value = self.data.get(oid)
        if value is not None:
            self.hits += 1
            self.order.remove(oid)
            self.order.append(oid)
        else:
            self.misses += 1
        return value

    def contains(self, oid):
        return oid in self.data

    def evict_to_fit(self, need):
        """Evict LRU entries until ``need`` more bytes fit beside the working set."""
        while self.order and self.resident + self.working + need > self.budget:
            victim = self.order.pop(0)
            value = self.data.pop(victim)
            self.resident -= len(value)
            self.evictions += 1
        self._snapshot()

    def put(self, oid, value):
        """Commit a verified in-memory object, evicting LRU entries as needed."""
        if len(value) > self.budget:
            raise ValueError('object larger than cache budget')
        if oid in self.data:
            old = self.data.pop(oid)
            self.resident -= len(old)
            self.order.remove(oid)
        self.evict_to_fit(len(value))
        self.data[oid] = value
        self.resident += len(value)
        self.order.append(oid)
        self._snapshot()

    def adopt_spooled(self, oid, material):
        """Commit a verified material that currently lives in a spool file."""
        if material.size > self.budget:
            raise ValueError('object larger than cache budget')
        self.evict_to_fit(material.size)
        spool = material.take_spool()
        value = spool.read(0, material.size)   # spool I/O: no ledger impact
        self.discard_spool(spool)
        self.data[oid] = value
        self.resident += len(value)
        self.order.append(oid)
        self._snapshot()

    def adopt_resident(self, oid, material):
        """Commit a verified material whose bytes are currently working set."""
        value = material.take_resident()       # detaches the working charge
        self.put(oid, value)
