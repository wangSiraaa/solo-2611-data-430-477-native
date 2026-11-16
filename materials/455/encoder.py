import json

def encode(values):
 # Existing writer uses a dictionary for every block, reserving zero for NULL.
 dictionary=list(dict.fromkeys(v for v in values if v is not None))
 return {'codec':'DICT','dictionary':dictionary,'codes':[0 if v is None else dictionary.index(v) for v in values]}
def byte_size(block):return len(json.dumps(block,ensure_ascii=False,separators=(',',':')).encode())
