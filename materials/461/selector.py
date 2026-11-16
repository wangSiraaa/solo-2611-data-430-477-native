def select(series,t,lookback):
 # Old selector skips stale markers and keeps searching an older number.
 rows=[s for s in series['samples'] if t-lookback<=s['t']<=t and 'v' in s]
 return max(rows,key=lambda s:s['t'])['v'] if rows else None
