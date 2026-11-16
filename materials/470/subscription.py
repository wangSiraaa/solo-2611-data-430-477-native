from collections import OrderedDict
class Missing(Exception):pass
class Subscription:
    def __init__(self,capacity=4):self.capacity=capacity;self.counter=0;self.retained=OrderedDict();self.acks=[]
    def notification(self,rows):
        self.counter=(self.counter%0xffffffff)+1
        m={'sequence':self.counter,'rows':list(rows)};self.retained[self.counter]=m
        while len(self.retained)>self.capacity:self.retained.popitem(last=False)
        return m
    def republish(self,sequence):
        if sequence not in self.retained:raise Missing(sequence)
        return self.retained[sequence]
    def acknowledge(self,sequence):self.acks.append(sequence);self.retained.pop(sequence,None)
