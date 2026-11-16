WIDTH=32
class Slices:
 def __init__(self): self.bits=[0]*WIDTH;self.live=0;self.nulls=0;self.slots={};self.next=0
 def put(self,rid,value):
  if rid not in self.slots:self.slots[rid]=self.next;self.next+=1
  pos=self.slots[rid];m=1<<pos;self.live|=m;self.nulls&=~m
  for b in range(WIDTH):self.bits[b]&=~m
  if value is None:self.nulls|=m
  else:
   for b in range(WIDTH):
    if (value&0xffffffff)>>b&1:self.bits[b]|=m
 def delete(self,rid):self.live&=~(1<<self.slots[rid])
