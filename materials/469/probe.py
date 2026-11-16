import json
from channel import Channel
from ledger import Ledger
from publisher import Publisher
c=Channel();l=Ledger();p=Publisher(c,l)
a=p.send('normal',{'x':1});c.emit('ack',tag=a);p.events();assert l.get('normal')[0]=='delivered'
b=p.send('badroute',{'x':2});c.emit('return',message_id='badroute',reply_code=312);c.emit('ack',tag=b);p.events()
print(json.dumps({'normal':l.get('normal'),'returned_then_ack':l.get('badroute'),'expected':'unroutable'}))
