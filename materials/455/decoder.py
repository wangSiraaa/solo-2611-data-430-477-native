def read(block,positions):
 if block['codec']=='PLAIN':return [block['values'][i] for i in positions]
 if block['codec']=='DICT':return [None if block['codes'][i]==0 else block['dictionary'][block['codes'][i]] for i in positions]
 raise ValueError('unknown codec')
