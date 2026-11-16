import json
from broker import Broker
from producer import Producer
from consumer import Consumer
b=Broker();p=Producer(b,'p');p.begin('a');p.send('k','old');p.commit();assert Consumer(b).read()[0]['value']=='old'
new=Producer(b,'p');new.begin('b');offset=new.send('k','new');new.commit()
p.begin('late');old_fenced=False
try:p.send('k','stale')
except Exception:old_fenced=True
print(json.dumps({'new_epoch_offset':offset,'expected_new_offset':1,'old_fenced':old_fenced,'log_values':[x['value'] for x in b.log]}))
