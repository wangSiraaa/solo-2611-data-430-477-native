class CodeMap:
    def __init__(self):self.objects={}
    def load(self,e):self.objects[e['code_id']]={'address':e['address'],'size':e['size'],'name':e['name'],'inline':e.get('inline',[])}
    def move(self,e):self.objects[e['code_id']]['address']=e['address']
    def unload(self,e):self.objects.pop(e['code_id'],None)
    def lookup(self,pc):
        for cid,o in self.objects.items():
            if o['address']<=pc<o['address']+o['size']:return cid,o,pc-o['address']
        return None
