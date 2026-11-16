import tempfile,json
from pathlib import Path
from tier_source import TierSource
from restore_manager import RestoreManager
from range_reader import RangeReader
with tempfile.TemporaryDirectory() as td:
 p=Path(td);(p/'x').write_bytes(b'abcdefgh');s=TierSource(p);m=RestoreManager(s);r=RangeReader(s,m)
 a=r.submit('a','x',0,2,20);b=r.submit('b','x',2,2,20);early=r.poll('a');earlyerror=r.errors.get('a');s.complete(a);s.complete(b);assert r.poll('a') and r.results['a']==b'ab'
 print(json.dumps({'restore_calls':s.restore_calls,'expected_one_shared':1,'pending_read_attempted':earlyerror,'early_completed':early,'normal_data':r.results['a'].decode()}))
