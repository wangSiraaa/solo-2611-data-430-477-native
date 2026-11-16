from binder import bind

def lateral(parents,provider,start,count,left=False):
 cache={}
 for parent in parents:
  key=(bind(start,parent),bind(count,parent))
  if key not in cache:cache[key]=provider.open(*key)
  found=False
  for child in cache[key]:found=True;yield parent|child
  if left and not found:yield parent|{'v':None}
