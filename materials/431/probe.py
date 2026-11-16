import socket,json,sys
from source import Server
from h2_transport import Client
from table_sink import TableSink
n=4 if sys.argv[1]=='normal' else 1000
rows={1:[{'row_id':i,'value':'x'*60} for i in range(n)],3:[{'row_id':i,'value':'y'*60} for i in range(n)]}
s=Server(rows);sink=TableSink();c=Client(sink);left,right=socket.socketpair();left.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,1024*1024);right.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,1024*1024);left.settimeout(.2);right.settimeout(.2)
def wire(conn,sock,other,receiver):
 data=conn.data_to_send()
 if not data:return False
 sock.sendall(data);got=b''
 while len(got)<len(data):got+=other.recv(len(data)-len(got))
 receiver(got);return True
try:
 wire(c.conn,left,right,s.receive);wire(s.conn,right,left,c.receive)
 for sid in rows:c.request(sid)
 wire(c.conn,left,right,s.receive)
 for tick in range(100):
  activity=s.step(reset_stream=1 if sys.argv[1]=='reset' else None)
  activity=wire(s.conn,right,left,c.receive) or activity
  activity=wire(c.conn,left,right,s.receive) or activity
  if not activity:break
 print(json.dumps({'states':sink.states,'committed_rows':{k:len(v) for k,v in sink.tables.items()},'pending_streams':list(s.pending),'sent_bytes':s.sent,'ticks':tick},sort_keys=True))
finally:left.close();right.close()
