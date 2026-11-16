class Consumer:
    def __init__(self,broker):self.broker=broker;self.position=0
    def read(self):
        result=[]
        with self.broker.lock:
            for row in self.broker.log[self.position:]:
                state=self.broker.transactions.get((row['producer'],row['tx']),'committed')
                if state=='committed':result.append(dict(row))
            self.position=len(self.broker.log)
        return result
