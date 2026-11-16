def estimate(sample):
 rows=[x for p in sample['pages'] for x in p['rows']]
 return sum(r['value'] for r in rows)/len(rows)*sample['N'] if rows else 0
