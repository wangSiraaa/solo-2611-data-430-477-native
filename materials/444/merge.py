class Replica:
    def __init__(self,sequence):self.sequence=sequence;self.events={}
    def merge(self,events):
        for event in events:
            eid=(event['replica'],event['seq'])
            if eid in self.events:continue
            if event['op']=='insert':self.sequence.insert(event)
            elif event['op']=='delete':self.sequence.delete(event['ids'])
            self.events[eid]=event
