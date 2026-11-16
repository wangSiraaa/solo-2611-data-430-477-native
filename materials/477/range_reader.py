class RangeReader:
    def __init__(self,source,manager):self.source=source;self.manager=manager;self.requests={};self.results={};self.errors={}
    def submit(self,id,key,offset,length,deadline):
        self.requests[id]=(key,offset,length,deadline);return self.manager.request(id,key)
    def poll(self,id):
        key,offset,length,deadline=self.requests[id]
        if not self.manager.ready(id):return False
        try:self.results[id]=self.source.range(key,offset,length)
        except Exception as e:self.errors[id]=type(e).__name__;return False
        self.manager.finish(id);self.requests.pop(id);return True
    def cancel(self,id):self.manager.cancel(id);self.requests.pop(id,None)
