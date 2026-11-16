import json
import zlib
from pathlib import Path

from cache import IO_CHUNK


class PackReader:
    """Reads zlib-compressed object payloads out of content-addressed packs.

    Every physical read goes through a fixed 8KiB I/O buffer (exempt from the
    working-set budget).  Each compressed read is recorded as evidence; reads
    performed while serving one request share that request id so callers can
    attribute real compressed-byte traffic to each delivery.
    """

    def __init__(self, root):
        self.root = Path(root)
        self.index = json.loads((self.root / 'index.json').read_text())
        self.reads = []
        self._request = None

    def entry(self, oid):
        return self.index[oid]

    def begin_request(self, oid):
        self._request = oid

    def end_request(self):
        self._request = None

    def _read_raw(self, entry):
        pack_path = self.root / entry['pack']
        offset = int(entry['offset'])
        length = int(entry['length'])
        raw = bytearray()
        with pack_path.open('rb') as f:
            f.seek(offset)
            remaining = length
            while remaining:
                buf = f.read(min(IO_CHUNK, remaining))
                if not buf:
                    raise EOFError('%s: pack truncated at offset %d' % (entry['pack'], offset))
                raw.extend(buf)
                remaining -= len(buf)
        if len(raw) != length:
            raise EOFError('%s: short read at offset %d' % (entry['pack'], offset))
        return bytes(raw), offset

    def _record(self, entry, raw_len, offset):
        self.reads.append({
            'request': self._request,
            'pack': entry['pack'],
            'offset': offset,
            'compressed_bytes': raw_len,
        })

    def payload(self, entry):
        raw, offset = self._read_raw(entry)
        d = zlib.decompressobj()
        out = d.decompress(raw)
        out += d.flush()
        if d.unused_data:
            raise ValueError('%s: trailing bytes after compressed payload' % entry['pack'])
        self._record(entry, len(raw), offset)
        return out

    def decompress_to(self, entry, sink):
        """Decompress an entry straight into file-like ``sink`` in 8KiB chunks.

        Used when the decompressed payload cannot fit alongside the current
        working set; only the fixed 8KiB buffers are ever resident.
        """
        raw, offset = self._read_raw(entry)
        d = zlib.decompressobj()
        view = memoryview(raw)
        produced = 0
        while view:
            chunk = d.decompress(view[:IO_CHUNK])
            if chunk:
                sink.write(chunk)
                produced += len(chunk)
            view = view[min(IO_CHUNK, len(view)):]
        tail = d.flush()
        if tail:
            sink.write(tail)
            produced += len(tail)
        if d.unused_data:
            raise ValueError('%s: trailing bytes after compressed payload' % entry['pack'])
        self._record(entry, len(raw), offset)
        return produced

    @property
    def compressed_bytes_total(self):
        return sum(r['compressed_bytes'] for r in self.reads)
