import threading,queue
class Sources:
    def __init__(self,frames,offsets=None):
        self.queues={};self.threads=[];self.frames=frames
        for name,rows in frames.items():
            q=queue.Queue(2);self.queues[name]=q;start=(offsets or {}).get(name,0)
            def produce(q=q,rows=rows,start=start):
                for n in range(start,len(rows)):q.put((n,rows[n]))
            t=threading.Thread(target=produce);self.threads.append(t);t.start()
    def receive(self,name):return self.queues[name].get(timeout=2)
    def close(self):
        for t in self.threads:t.join(timeout=2)
