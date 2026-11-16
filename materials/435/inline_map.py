def resolve(obj,offset):
    candidates=[r for r in obj['inline'] if r['start']<=offset<r['end']]
    if candidates:
        leaf=min(candidates,key=lambda r:r['end']-r['start'])
        return [leaf['name']]
    return [obj['name']]
