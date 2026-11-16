class Collector:
    def __init__(self,requester):self.requester=requester;self.rows={};self.cursors={};self.pages={}
    def collect(self,stream):
        cursor=self.cursors.get(stream,0)
        while cursor is not None:
            page=self.requester.page(stream,cursor)
            self.rows.setdefault(stream,[]).extend(page['rows']);self.pages[page['page_id']]=len(page['rows'])
            cursor=page['next'];self.cursors[stream]=cursor
        return self.rows[stream]
