from selector import select
from matcher import match

def instant(series,t,lookback):
 out=[]
 for s in series:
  v=select(s,t,lookback)
  if v is not None:out.append({'labels':s['labels'],'v':v})
 return out

def evaluate(left,right,t,lookback,on):return match(instant(left,t,lookback),instant(right,t,lookback),on)
