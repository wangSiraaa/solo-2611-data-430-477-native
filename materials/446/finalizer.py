from collections import defaultdict

def finish(pool,limit=4096,distinct=False):
    groups=defaultdict(list)
    for state in pool.states():groups[state['group']].extend(state['text'].split('|'))
    for g,values in sorted(groups.items()):
        if distinct: values=list(dict.fromkeys(values))
        encoded='|'.join(values).encode('utf8')
        yield {'group':g,'text':encoded[:limit].decode('utf8',errors='ignore'),'omitted':max(0,len(encoded)-limit)}
