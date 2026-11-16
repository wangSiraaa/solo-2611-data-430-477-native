class Book:
    def __init__(self):
        self.sequence=None;self.levels={};self.sources=[];self.status='loading'
    def snapshot(self, data):
        self.sequence=data['sequence'];self.levels={(s,int(p)):int(q) for s,p,q in data['levels']};self.status='ready';self.sources=[data['id']]
    def delta(self, data):
        if self.sequence is None:
            return
        for side,price,quantity in data['levels']:
            key=(side,int(price))
            if quantity:self.levels[key]=int(quantity)
            else:self.levels.pop(key,None)
        self.sequence=data['last'];self.sources.append(data['id'])
    def view(self):
        return {'sequence':self.sequence,'status':self.status,'levels':[[s,p,q] for (s,p),q in sorted(self.levels.items())],'sources':list(self.sources)}
