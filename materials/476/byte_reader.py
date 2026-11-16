class ByteReader:
    def __init__(self,device):self.device=device
    def read(self,offset,length):
        size=self.device.size;out=bytearray();at=0
        while at<length:
            sector,inside=divmod(offset+at,size);n=min(size-inside,length-at);block=self.device.read(sector);out.extend(block[inside:inside+n]);at+=n
        return bytes(out)
