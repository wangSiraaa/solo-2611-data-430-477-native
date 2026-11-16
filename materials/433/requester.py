from service import Unauthorized
class Requester:
    def __init__(self,source,auth):self.source=source;self.auth=auth
    def page(self,stream,page,expired=False):
        token=self.auth.token()
        try:return self.source.get(stream,page,token,expired)
        except Unauthorized:
            fresh=self.auth.refresh_after_401(token)
            return self.source.get(stream,page,fresh)
