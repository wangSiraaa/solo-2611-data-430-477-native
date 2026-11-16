"""Nullable UTF-8 single-column batch ABI, little-endian 32-bit fields."""
import struct
HEADER=struct.Struct('<8I')
def encode(rows,batch_no,final,address):
    validity=bytearray((len(rows)+7)//8);data=bytearray();offsets=[0]
    for n,value in enumerate(rows):
        if value is not None:validity[n//8]|=1<<(n%8);data.extend(value.encode('utf-8'))
        offsets.append(len(data))
    offptr=address+HEADER.size;validptr=offptr+4*len(offsets);dataptr=validptr+len(validity)
    header=HEADER.pack(1,batch_no,len(rows),offptr,validptr,dataptr,len(data),int(final))
    return header+struct.pack('<'+'I'*len(offsets),*offsets)+validity+data

def decode(memory,store,address):
    header=bytes(memory.read(store,address,address+HEADER.size))
    version,batch,n,offsetptr,validptr,dataptr,size,flags=HEADER.unpack(header)
    offsets=struct.unpack('<'+'I'*(n+1),memory.read(store,offsetptr,offsetptr+4*(n+1)))
    validity=bytes(memory.read(store,validptr,validptr+(n+7)//8))
    data=bytes(memory.read(store,dataptr,dataptr+size))
    values=[]
    for i in range(n):
        values.append(data[offsets[i]:offsets[i+1]].decode('utf-8') if validity[i//8]&(1<<(i%8)) else None)
    return {'batch_no':batch,'final':bool(flags&1),'rows':values}
