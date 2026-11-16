import tempfile,threading,json
from pathlib import Path
from device import Device
from accounting import Accounting
from scheduler import Scheduler
with tempfile.TemporaryDirectory() as td:
 p=Path(td)/'data';p.write_bytes(b'x'*16384);dev=Device(p);a=Accounting({'A':1,'B':1});s=Scheduler(dev,a,1);gate=threading.Event()
 s.submit('a','A',0,4096,gate);s.tick();s.cancel('a');s.submit('b','B',4096,4096,gate);s.tick();peak=dev.peak;gate.set();dev.join()
 print(json.dumps({'physical_peak':peak,'limit':1,'served':a.served,'started':dev.started}));dev.close()
