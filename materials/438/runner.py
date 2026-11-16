import wasmtime
from abi import encode,decode
from sink import Sink
class Runner:
    def __init__(self):
        cfg=wasmtime.Config();cfg.consume_fuel=True
        self.engine=wasmtime.Engine(cfg)
    def run(self,wat,batches,path,fuel=100000):
        store=wasmtime.Store(self.engine);store.set_fuel(fuel)
        module=wasmtime.Module(self.engine,wat);instance=wasmtime.Instance(store,module,[]);exports=instance.exports(store)
        memory=exports['memory'];sink=Sink(path);calls=[]
        try:
            for n,rows in enumerate(batches):
                provisional=encode(rows,n,n==len(batches)-1,0);address=exports['alloc'](store,len(provisional))
                packed=encode(rows,n,n==len(batches)-1,address);memory.write(store,packed,address)
                result=exports['process'](store,address,n,int(n==len(batches)-1));calls.append(('process',n,result,memory.size(store)))
                batch=decode(memory,store,result);sink.append(batch)
                exports['release'](store,result);exports['dealloc'](store,address,len(packed));calls.append(('release',n))
            return {'status':'complete','product':sink.inspect(),'calls':calls}
        except Exception as exc:
            return {'status':'failed','reason':type(exc).__name__,'product':sink.inspect(),'calls':calls}
