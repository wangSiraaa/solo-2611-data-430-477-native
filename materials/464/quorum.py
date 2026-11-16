import socket,json
from concurrent.futures import ThreadPoolExecutor,as_completed
class Transport:
    def __init__(self,addresses):self.addresses=addresses;self.calls=[]
    def rpc(self,node,request):
        self.calls.append((node,request.copy()))
        with socket.create_connection(self.addresses[node],timeout=.2) as s:
            s.sendall(json.dumps(request).encode()+b'\n');data=s.makefile('rb').readline()
        return json.loads(data)
    def majority(self,request,nodes=None):
        nodes=list(nodes or self.addresses);results=[]
        with ThreadPoolExecutor(len(nodes)) as ex:
            fs=[ex.submit(self.rpc,n,request) for n in nodes]
            for f in as_completed(fs):
                try:results.append(f.result())
                except OSError:continue
                if len(results)>=2:return results
        raise TimeoutError('no majority')
