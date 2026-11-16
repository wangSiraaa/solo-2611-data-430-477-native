from proofs import verify_inclusion
class Witness:
    def __init__(self,size,root):self.size=size;self.root=root;self.accepted=[]
    def accept(self,new_size,new_root,proof):
        if new_size<self.size:raise ValueError('rollback')
        if not verify_inclusion(bytes.fromhex(proof['first']),proof['path'],new_root):raise ValueError('bad proof')
        self.size=new_size;self.root=new_root;self.accepted.append((new_size,new_root));return True
