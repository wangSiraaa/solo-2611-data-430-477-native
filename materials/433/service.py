import threading
class Unauthorized(Exception):pass
class InvalidRefresh(Exception):pass
class Source:
    def __init__(self):
        self.access='a0';self.refresh='r0';self.generation=0;self.lock=threading.Lock();self.refresh_calls=0
        self.pages={'A':[['a1','a2'],['a3']], 'B':[['b1'],['b2','b3']]}
    def get(self, stream, page, token, expired=False):
        if expired or token!=self.access:raise Unauthorized(token)
        rows=self.pages[stream][page]
        return {'page_id':f'{stream}:{page}','rows':rows,'complete':True,'next':page+1 if page+1<len(self.pages[stream]) else None}
    def rotate(self, refresh):
        with self.lock:
            self.refresh_calls+=1
            if refresh!=self.refresh:raise InvalidRefresh(refresh)
            self.generation+=1;self.access=f'a{self.generation}';self.refresh=f'r{self.generation}'
            return self.access,self.refresh
