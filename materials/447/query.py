def ge(index,bound):
 # Unsigned lexicographic compare from the high plane.
 equal=index.live & ~index.nulls;greater=0
 for bit in range(31,-1,-1):
  ones=index.bits[bit];b=(bound&0xffffffff)>>bit&1
  if b:equal &= ones
  else:greater |= equal&ones;equal &= ~ones
 return greater|equal

def ids(index,mask):return sorted(rid for rid,pos in index.slots.items() if mask>>pos&1)
def total(index,mask):return sum((index.bits[b]&mask).bit_count()*(1<<b) for b in range(32))
def not_mask(index,mask):return ((1<<((index.next+63)//64*64))-1)^mask
