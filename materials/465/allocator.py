class Store:
    def __init__(self,zones,index):self.zones=zones;self.index=index;self.active=0
    def put(self,key,data):
        try:loc=self.zones.append(self.active,data)
        except BufferError:
            self.active=(self.active+1)%self.zones.count
            self.zones.reset(self.active)
            loc=self.zones.append(self.active,data)
        self.index.put(key,loc)
    def get(self,key):return self.zones.read(self.index.get(key)[1])
    def clean(self,zone):self.zones.reset(zone)
