def can_read(reader,writer):
    if reader['type']==writer['type']:
        if reader['type']!='record':return True
        old={f['name']:f for f in writer['fields']}
        return all(f['name'] in old or 'default' in f for f in reader['fields'])
    return reader['type']=='long' and writer['type']=='int'
class Registry:
    def __init__(self,catalog,resolver):self.catalog=catalog;self.resolver=resolver
    def register(self,subject,schema,mode='BACKWARD_TRANSITIVE'):
        try:_,old=self.catalog.get(subject)
        except KeyError:return self.catalog.publish(subject,schema)
        if not can_read(self.resolver.expand(schema),self.resolver.expand(old)):raise ValueError('incompatible')
        return self.catalog.publish(subject,schema)
