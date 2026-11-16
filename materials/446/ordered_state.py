from collections import defaultdict

def partial(rows, chunk):
    groups=defaultdict(list)
    for r in rows: groups[r['group']].append(r)
    # The original interface treats rendered text as a mergeable state.
    return [{'chunk':chunk,'group':g,'text':'|'.join(r['value'] for r in sorted(rs,key=lambda x:(x['key'] is None,x['key'] or 0,x['id']))),'count':len(rs)} for g,rs in groups.items()]
