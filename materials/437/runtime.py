import hashlib,threading,queue
from state import State

def key_group(key):return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8],'big')%16
class Worker:
    def __init__(self,state,publish):
        self.state=state;self.publish=publish;self.q=queue.Queue();self.thread=threading.Thread(target=self.run);self.thread.start()
    def run(self):
        while True:
            item=self.q.get()
            try:
                if item is None:return
                op,args=item
                if op=='add':self.state.add(*args)
                elif op=='timer':self.state.timer(*args)
                elif op=='watermark':
                    for result in self.state.advance(*args):self.publish(result)
            finally:self.q.task_done()
    def close(self):self.q.put(None);self.q.join();self.thread.join()
class Runtime:
    def __init__(self,count):self.outputs=[];self.workers=[Worker(State(),self.publish) for _ in range(count)]
    def publish(self,result):self.outputs.append(result)
    def submit(self,op,args):self.workers[key_group(args[0] if op=='add' else args[1])%len(self.workers)].q.put((op,args))
    def drain(self):
        for w in self.workers:w.q.join()
    def watermark(self,time):
        for w in self.workers:w.q.put(('watermark',(time,)))
        self.drain()
    def rescale(self,path,count):
        from savepoint import save,restore
        self.drain();save([w.state for w in self.workers],path)
        old=self.workers;self.workers=[Worker(s,self.publish) for s in restore(path,count,key_group)]
        for w in old:w.close()
    def close(self):
        for w in self.workers:w.close()
