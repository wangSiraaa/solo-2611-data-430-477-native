from pathlib import Path
import zlib,struct,json,hashlib
from resolver import object_id

def build(root):
 root=Path(root);root.mkdir(exist_ok=True);packs={'a.pack':bytearray(),'b.pack':bytearray()};index={}
 def add(data,kind='full',base=None,ops=None,pack='a.pack'):
  oid=object_id(data);payload=data if kind=='full' else ops;raw=zlib.compress(payload);b=packs[pack];index[oid]={'pack':pack,'offset':len(b),'length':len(raw),'kind':kind,'size':len(data),'base':base};b.extend(raw);return oid
 base=b'hello world';full=add(base);local=add(base+b'!',kind='delta',base=full,ops=b'\x01'+struct.pack('>II',0,len(base))+b'\x02'+struct.pack('>I',1)+b'!\0')
 cross=add(base+b'?',kind='delta',base=full,ops=b'\x01'+struct.pack('>II',0,len(base))+b'\x02'+struct.pack('>I',1)+b'?\0',pack='b.pack')
 chain=add(base+b'?!',kind='delta',base=cross,ops=b'\x01'+struct.pack('>II',0,len(base)+1)+b'\x02'+struct.pack('>I',1)+b'!\0',pack='b.pack')
 bulk=[add(bytes([i])*65536,pack='b.pack') for i in range(8)]
 for name,data in packs.items():(root/name).write_bytes(data)
 (root/'index.json').write_text(json.dumps(index,indent=2));(root/'requests.json').write_text(json.dumps({'normal':[full,local],'cross':[cross,chain],'budget':bulk}))
if __name__=='__main__':build('fixtures')
