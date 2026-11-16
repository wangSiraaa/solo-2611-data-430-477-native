import json,hashlib
class TableSink:
    def __init__(self):self.pending={};self.tables={};self.states={};self.expected={};self.buffers={}
    def start(self,stream,headers):
        self.pending[stream]=[];self.buffers[stream]=b'';self.expected[stream]=dict(headers).get('x-row-count');self.states[stream]='receiving'
    def consume(self,stream,data):
        self.buffers[stream]+=data
        while b'\n' in self.buffers[stream]:
            line,self.buffers[stream]=self.buffers[stream].split(b'\n',1)
            if line:self.pending[stream].append(json.loads(line))
    def finish(self,stream,reset=False):
        self.tables[stream]=self.pending.pop(stream);self.states[stream]='complete'
