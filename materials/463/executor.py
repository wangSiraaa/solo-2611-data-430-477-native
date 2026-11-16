import threading
class Query:
    def __init__(self,pool,lease,sql):
        self.pool=pool;self.lease=lease;self.sql=sql
        self.rows=[];self.error=None;self.done=threading.Event()
    def start(self,pause=None):
        def run():
            try:
                for row in self.lease.session.rows(self.sql):
                    self.rows.append(row)
                    if pause:pause.wait()
            except Exception as exc:self.error=str(exc)
            finally:self.done.set()
        self.worker=threading.Thread(target=run,daemon=True);self.worker.start();return self
    def cancel(self):self.lease.session.interrupt()
    def return_connection(self):self.pool.release(self.lease)
