import struct,hashlib

def object_id(data):return hashlib.sha256(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
class Resolver:
    def __init__(self,reader,cache):self.reader=reader;self.cache=cache
    def read(self,oid):
        cached=self.cache.get(oid)
        if cached is not None:return cached
        entry=self.reader.entry(oid);payload=self.reader.payload(entry)
        if entry['kind']=='full':value=payload
        else:
            # Initial implementation assumes the base is a full object in this pack.
            base_entry=self.reader.entry(entry['base'])
            if base_entry['pack']!=entry['pack'] or base_entry['kind']!='full':raise ValueError('base not local full')
            base=self.reader.payload(base_entry);out=bytearray();at=0
            while payload[at]:
                op=payload[at];at+=1
                if op==1:
                    start,n=struct.unpack_from('>II',payload,at);at+=8;out.extend(base[start:start+n])
                elif op==2:
                    n=struct.unpack_from('>I',payload,at)[0];at+=4;out.extend(payload[at:at+n]);at+=n
                else:raise ValueError('bad op')
            value=bytes(out)
        self.cache.put(oid,value);return value
