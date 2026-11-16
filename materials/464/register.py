class Register:
    def __init__(self,transport):self.transport=transport;self.next_tag=0
    def write(self,value):
        self.next_tag+=1;self.transport.majority({'op':'put','tag':self.next_tag,'value':value});return self.next_tag
    def read(self,nodes=None):
        versions=self.transport.majority({'op':'get'},nodes)
        best=max(versions,key=lambda r:r['tag'])
        return best['value']
