from auth import ReauthenticationRequired
from service import Unauthorized

MAX_AUTH_ATTEMPTS = 3


class Requester:
    """页请求器：GET 可安全重放，401 交给 Auth 单飞处理后重放。

    本层不持有任何覆盖请求过程的锁；多个正常 GET 真正并行，
    仅在 Auth 内部的条件变量上短暂同步刷新代际。
    """

    def __init__(self, source, auth):
        self.source = source
        self.auth = auth

    def page(self, stream, page, expired=False, token=None):
        """获取一页。token 仅供测试注入迟到 401 场景，正常路径自取当前 token。"""
        attempts = 0
        while True:
            used = token if token is not None else self.auth.token()
            try:
                return self.source.get(stream, page, used, expired=expired)
            except Unauthorized:
                attempts += 1
                if attempts > MAX_AUTH_ATTEMPTS:
                    raise ReauthenticationRequired(
                        f"401 loop after {MAX_AUTH_ATTEMPTS} rotations"
                    )
                # 同代共享刷新结果；迟到旧代 401 不会重复消费 refresh；
                # 认证域停止时这里一致抛出 ReauthenticationRequired。
                fresh = self.auth.refresh_after_401(used)
                token = None
                used = fresh
