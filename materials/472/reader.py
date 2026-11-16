def apply(value,record):
    if record['op']=='put':return record['value']
    if record['op']=='delete':return None
    if record['op']=='add':return (value or 0)+record['value']
    if record['op']=='mul':return (value or 0)*record['value']
    raise ValueError(record['op'])
def get(tables,key,snapshot):
    rows=[r for t in tables for r in t.records(key) if r['seq']<=snapshot]
    if not rows:return None
    latest=max(rows,key=lambda x:x['seq'])
    return apply(None,latest)
