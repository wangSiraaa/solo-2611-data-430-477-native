class Publisher:
    def __init__(self,channel,ledger):self.channel=channel;self.ledger=ledger;self.pending={}
    def send(self,id,payload):
        self.ledger.add(id,payload);tag=self.channel.publish({'message_id':id,'payload':payload});self.pending[tag]=id;self.ledger.sent(id,tag);return tag
    def events(self):
        for event in self.channel.drain():
            if event['type'] in ('ack','nack'):
                tag=event['tag'];tags=[t for t in self.pending if t<=tag] if event.get('multiple') else [tag]
                for t in tags:
                    id=self.pending.pop(t,None)
                    if id:self.ledger.status(id,'delivered' if event['type']=='ack' else 'failed')
            elif event['type']=='return':
                pass
            elif event['type']=='close':
                for id in self.pending.values():self.ledger.status(id,'failed','closed')
                self.pending.clear()
