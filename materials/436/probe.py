import json
import sys

from cache import Cache
from pack_reader import PackReader
from resolver import Resolver, object_id

BUDGET = 256 * 1024

r = PackReader('fixtures')
c = Cache(BUDGET)
resolver = Resolver(r, c)
requests = json.load(open('fixtures/requests.json'))[sys.argv[1]]
result = []
for oid in requests:
    was_cached = c.contains(oid)
    reads_before = len(r.reads)
    r.begin_request(oid)
    try:
        value = resolver.read(oid)
        result.append({
            'oid': oid,
            'bytes': len(value),
            'hash_matches': object_id(value) == oid,
            'cache_hit': was_cached,
            'pack_compressed_bytes': sum(
                x['compressed_bytes'] for x in r.reads[reads_before:]),
        })
    except Exception as e:
        result.append({'oid': oid, 'error': str(e)})
    finally:
        r.end_request()
print(json.dumps({
    'results': result,
    'budget': c.budget,
    'working_set_peak': c.peak,
    'resident_peak': c.resident_peak,
    'cache_hits': c.hits,
    'cache_misses': c.misses,
    'cache_evictions': c.evictions,
    'temp_bytes_written': c.temp_written,
    'temp_bytes_read': c.temp_read,
    'compressed_bytes_total': r.compressed_bytes_total,
    'reads': r.reads,
    'errors': resolver.errors,
}, sort_keys=True))
