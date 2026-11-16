class Resolver:
    def __init__(self,catalog):self.catalog=catalog
    def expand(self,schema,stack=()):
        if schema['type']=='ref':
            subject=schema['subject']
            if subject in stack:raise ValueError('cycle')
            _,target=self.catalog.get(subject)
            return self.expand(target,stack+(subject,))
        if schema['type']=='record':return {**schema,'fields':[{**f,'schema':self.expand(f['schema'],stack)} for f in schema['fields']]}
        return schema
