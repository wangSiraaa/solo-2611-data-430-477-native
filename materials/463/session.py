import sqlite3
class Session:
    def __init__(self):
        self.db=sqlite3.connect(':memory:', check_same_thread=False)
        self.tenant=None
        self.cursors=set()
    def bind(self, tenant, path):
        if self.tenant is not None:
            self.db.execute('DETACH DATABASE tenant')
        self.db.execute('ATTACH DATABASE ? AS tenant',(str(path),))
        self.tenant=tenant
    def rows(self,sql,params=()):
        cursor=self.db.execute(sql,params)
        self.cursors.add(cursor)
        try:
            for row in cursor: yield row
        finally:
            cursor.close();self.cursors.discard(cursor)
    def reset(self):
        self.db.rollback()
    def interrupt(self):self.db.interrupt()
    def close(self):self.db.close()
