from collections import deque
class Channel:
    def __init__(self):self.sequence=0;self.events=deque();self.published=[]
    def publish(self,message,mandatory=True):
        self.sequence+=1;self.published.append((self.sequence,message,mandatory));return self.sequence
    def emit(self,type,**fields):self.events.append(dict(type=type,**fields))
    def drain(self):
        while self.events:yield self.events.popleft()
