"""Working model directory with default one-way relationship propagation."""
from dataclasses import dataclass, field
from copy import deepcopy

@dataclass
class Context:
    filters: dict = field(default_factory=dict)
    row_stack: tuple = ()
    relationships: dict = field(default_factory=dict)
    def copy(self):
        return Context(dict(self.filters),tuple(self.row_stack),dict(self.relationships))

class Model:
    def __init__(self, document):
        self.schemas=deepcopy(document['schemas'])
        self.tables=deepcopy(document['tables'])
        self.relations=deepcopy(document['relationships'])
        self.measures=deepcopy(document['measures'])
        self.active={r['name']:r['active'] for r in self.relations}
        self.audit=[]
        for table,rows in self.tables.items():
            key=self.schemas[table]['key']; ids=[r[key] for r in rows]
            if len(ids)!=len(set(ids)):raise ValueError('DUPLICATE_KEY:'+table)
    def visible(self,table,context):
        memo={}
        def visit(name):
            if name in memo:return memo[name]
            rows=[r for r in self.tables[name] if all(r[col] in values for (t,col),values in context.filters.items() if t==name)]
            for relation in self.relations:
                if relation['child_table']!=name or not self.active[relation['name']]:continue
                parent=relation['parent_table']; parent_rows=visit(parent)
                # No parent restriction means the relationship does not remove NULL foreign keys.
                if len(parent_rows)!=len(self.tables[parent]):
                    keys={r[relation['parent_column']] for r in parent_rows}
                    rows=[r for r in rows if r[relation['child_column']] in keys]
            memo[name]=rows;return rows
        return visit(table)
    def aggregate(self,op,table,column,context):
        rows=self.visible(table,context)
        self.audit.append({'op':op,'table':table,'row_ids':[r[self.schemas[table]['key']] for r in rows],
                           'active_relationships':[n for n,a in sorted(self.active.items()) if a]})
        if op=='countrows':return len(rows)
        values=[r[column] for r in rows if r[column] is not None]
        return sum(values) if values else None
