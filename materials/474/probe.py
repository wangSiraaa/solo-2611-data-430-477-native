import json
from catalog import Catalog
from resolver import Resolver
from compatibility import Registry
c=Catalog();r=Resolver(c);g=Registry(c,r);c.publish('D',{'type':'int'});ref={'type':'ref','subject':'D','version':1};assert r.expand(ref)['type']=='int'
c.publish('D',{'type':'string'});resolved=r.expand(ref)['type'];g.register('R',{'type':'record','name':'R','fields':[{'name':'a','schema':{'type':'int'}}]})
accepted=True
try:g.register('R',{'type':'record','name':'R','fields':[{'name':'a','schema':{'type':'string'}}]})
except ValueError:accepted=False
print(json.dumps({'pinned_ref_after_new_version':resolved,'expected':'int','nested_incompatible_accepted':accepted}))
