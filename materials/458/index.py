class Index:
 def __init__(self,column):self.column=column;self.mapping={}
 def add(self,id,row):self.mapping.setdefault(row.get(self.column),set()).add(id)
 def find(self,value):return sorted(self.mapping.get(value,set()))
