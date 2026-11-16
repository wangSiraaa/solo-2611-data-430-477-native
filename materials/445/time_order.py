def leq(a,b):return tuple(a)<=tuple(b)
def canonical(frontier):return [list(x) for x in sorted(set(map(tuple,frontier)))]
def frontier_leq(a,b):return all(any(leq(x,y) for x in a) for y in b)
def complete(frontier,time):return not any(leq(f,time) for f in frontier)
