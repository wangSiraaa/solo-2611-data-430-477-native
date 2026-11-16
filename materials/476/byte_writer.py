class ByteWriter:
    def __init__(self,device):self.device=device
    def write(self,offset,data,after_read=None,after_sector=None):
        size=self.device.size;at=0
        while at<len(data):
            sector,inside=divmod(offset+at,size);n=min(size-inside,len(data)-at)
            block=bytearray(self.device.read(sector))
            if after_read:after_read(sector)
            block[inside:inside+n]=data[at:at+n];self.device.write(sector,block)
            if after_sector:after_sector(sector)
            at+=n
