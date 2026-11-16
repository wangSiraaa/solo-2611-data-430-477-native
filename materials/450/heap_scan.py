import json
from pathlib import Path

def scan(root,bitmap,predicate):
 out=[];audit=[]
 for p,offsets in bitmap.pages.items():
  rows=json.loads((Path(root)/f'{p}.json').read_text());audit.append({'page':p,'lossy':offsets is None})
  for off,row in enumerate(rows):
   if offsets is None:
    if predicate(row):out.append(row['row_id'])
   elif off in offsets:out.append(row['row_id'])
 return {'ids':sorted(out),'reads':audit}
