import random

def select(catalog,k,seed):return random.Random(seed).sample(sorted(catalog['pages']),k)
def collect(source,k,seed):
 c=source.catalog();pages=select(c,k,seed)
 return {'source':c['source'],'N':c['rows'],'M':len(c['pages']),'k':k,'seed':seed,'pages':[{'id':id,'rows':source.page(id)} for id in pages]}
