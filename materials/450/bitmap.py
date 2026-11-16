class Bitmap:
 def __init__(self):self.pages={}
 def add(self,page,offset):
  self.pages.setdefault(page,set())
  if self.pages[page] is not None:self.pages[page].add(offset)
 def charge(self):return sum(4+(0 if x is None else 2*len(x)) for x in self.pages.values())
 def fit(self,budget):
  for page in sorted(self.pages,key=lambda p:len(self.pages[p] or ()),reverse=True):
   if self.charge()<=budget:break
   self.pages[page]=None
  if self.charge()>budget:raise ValueError('budget cannot hold page directory')
