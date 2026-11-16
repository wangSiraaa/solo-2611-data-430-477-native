import json,zlib
class Summary:
    def __init__(self,users=()):self.users=set(users)
    def add(self,user):self.users.add(user)
    def remove(self,user):self.users.discard(user)
    def merge(self,other):self.users|=other.users
    def count(self):return len(self.users)
    def payload(self):return zlib.compress(json.dumps(sorted(self.users),separators=(',',':')).encode())
