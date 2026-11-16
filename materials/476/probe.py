import tempfile,threading,json
from pathlib import Path
from device import Device
from byte_writer import ByteWriter
from byte_reader import ByteReader
with tempfile.TemporaryDirectory() as td:
 d=Device(str(Path(td)/'dev'));w=ByteWriter(d);r=ByteReader(d);w.write(5,b'N');assert r.read(5,1)==b'N'
 barrier=threading.Barrier(2)
 a=threading.Thread(target=lambda:w.write(0,b'A',after_read=lambda _:barrier.wait()));b=threading.Thread(target=lambda:w.write(1,b'B',after_read=lambda _:barrier.wait()));a.start();b.start();a.join();b.join()
 actual=r.read(0,2).hex();intermediate=[];readers=[]
 def observe(sec):
  if sec!=0:return
  thread=threading.Thread(target=lambda:intermediate.append(r.read(127,2).hex()));readers.append(thread);thread.start();thread.join(.2)
 w.write(127,b'XY',after_sector=observe)
 for thread in readers:thread.join(1)
 print(json.dumps({'same_sector_actual':actual,'expected':'4142','cross_sector_intermediate':intermediate,'legal_before':'0000','legal_after':'5859'}));d.close()
