import json,hashlib
from h2.connection import H2Connection
from h2.config import H2Configuration
from h2.events import RequestReceived
class Server:
    def __init__(self,rows):
        self.conn=H2Connection(H2Configuration(client_side=False,header_encoding='utf-8'));self.conn.initiate_connection();self.rows=rows;self.pending={};self.sent={};self.reset=set()
    def receive(self,data):
        for e in self.conn.receive_data(data):
            if isinstance(e,RequestReceived):
                data=b''.join(json.dumps(r).encode()+b'\n' for r in self.rows[e.stream_id]);self.pending[e.stream_id]=data;self.sent[e.stream_id]=0
                self.conn.send_headers(e.stream_id,[(':status','200'),('x-row-count',str(len(self.rows[e.stream_id]))),('x-table-id',str(e.stream_id)),('x-content-sha256',hashlib.sha256(data).hexdigest())])
    def step(self,reset_stream=None):
        progressed=False
        for sid,data in list(self.pending.items()):
            credit=min(self.conn.local_flow_control_window(sid),self.conn.max_outbound_frame_size,8192)
            n=min(credit,len(data))
            if n:
                self.conn.send_data(sid,data[:n],end_stream=n==len(data));self.sent[sid]+=n;progressed=True
                self.pending[sid]=data[n:]
                if n==len(data):del self.pending[sid]
                elif reset_stream==sid:
                    self.conn.reset_stream(sid,error_code=8);del self.pending[sid];self.reset.add(sid)
        return progressed
