from h2.connection import H2Connection
from h2.config import H2Configuration
from h2.events import ResponseReceived,DataReceived,StreamEnded,StreamReset
from h2.exceptions import StreamClosedError
class Client:
    def __init__(self,sink):
        self.conn=H2Connection(H2Configuration(client_side=True,header_encoding='utf-8'));self.sink=sink;self.events=[];self.conn.initiate_connection()
    def request(self,stream):self.conn.send_headers(stream,[(':method','GET'),(':scheme','http'),(':authority','local'),(':path',f'/table/{stream}')],end_stream=True)
    def receive(self,data):
        for e in self.conn.receive_data(data):
            self.events.append(type(e).__name__)
            if isinstance(e,ResponseReceived):self.sink.start(e.stream_id,e.headers)
            elif isinstance(e,DataReceived):
                self.sink.consume(e.stream_id,e.data)
                try:self.conn.increment_flow_control_window(e.flow_controlled_length,stream_id=e.stream_id)
                except StreamClosedError:pass
            elif isinstance(e,StreamEnded):self.sink.finish(e.stream_id)
            elif isinstance(e,StreamReset):self.sink.finish(e.stream_id,reset=True)
