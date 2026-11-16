class Auth:
    def __init__(self, source):self.source=source;self.access=source.access;self.refresh=source.refresh
    def token(self):return self.access
    def refresh_after_401(self, used_token, after_read=None):
        old=self.refresh
        if after_read:after_read()
        access,refresh=self.source.rotate(old)
        self.access,self.refresh=access,refresh
        return access
