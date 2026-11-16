class Annotations:
    def __init__(self):self.spans={}
    def add(self,id,start,end,label):self.spans[id]={'start':start,'end':end,'label':label}
    def export(self,sequence):
        text=sequence.text();return [{'id':id,**s,'quote':text[s['start']:s['end']]} for id,s in sorted(self.spans.items())]
