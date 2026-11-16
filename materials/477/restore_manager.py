class RestoreManager:
    def __init__(self,source):self.source=source;self.tickets={};self.waiters={}
    def request(self,id,key):
        ticket=self.source.restore(key);self.tickets[key]=ticket;self.waiters[id]=(key,ticket);return ticket
    def ready(self,id):
        key,ticket=self.waiters[id];return self.source.status(ticket)['state']!='failed'
    def cancel(self,id):
        key,ticket=self.waiters.pop(id);self.tickets.pop(key,None)
    def finish(self,id):self.waiters.pop(id,None)
