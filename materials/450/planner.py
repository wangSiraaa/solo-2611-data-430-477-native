from bitmap import Bitmap

def combine(a,b,op):
 out=Bitmap()
 pages=set(a.pages)|set(b.pages) if op=='or' else set(a.pages)&set(b.pages)
 for p in pages:
  x=a.pages.get(p,set());y=b.pages.get(p,set())
  if x is None:out.pages[p]=y
  elif y is None:out.pages[p]=x
  else:out.pages[p]=(x|y if op=='or' else x&y)
 return out
