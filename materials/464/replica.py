import json,threading,socketserver
class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        q=json.loads(self.rfile.readline());s=self.server
        with s.lock:
            if q['op']=='put' and q['tag']>=s.tag:s.tag=q['tag'];s.value=q['value']
            result={'tag':s.tag,'value':s.value}
        self.wfile.write(json.dumps(result).encode()+b'\n')
class Replica(socketserver.ThreadingTCPServer):
    allow_reuse_address=True;daemon_threads=True
    def __init__(self):super().__init__(('127.0.0.1',0),Handler);self.tag=0;self.value='initial';self.lock=threading.Lock()
    def start(self):self.worker=threading.Thread(target=self.serve_forever,daemon=True);self.worker.start();return self.server_address
