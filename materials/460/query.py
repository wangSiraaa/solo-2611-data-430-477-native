import math

def intervals(h):
 base=2**(2**(-h['scale']));out=[]
 for side in ('negative','positive'):
  for k,n in sorted(h[side].items(),key=lambda x:int(x[0])):
   lo=base**int(k);hi=base**(int(k)+1)
   out.append(((-hi,-lo) if side=='negative' else (lo,hi),n))
 if h['zero']:out.append(((-h['zero_threshold'],h['zero_threshold']),h['zero']))
 return out

def quantile(h,q):
 rank=max(1,math.ceil(q*h['count']));c=0
 for interval,n in intervals(h):
  c+=n
  if c>=rank:return interval
 return None
