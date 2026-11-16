import threading

from service import InvalidRefresh, UnknownRotationOutcome


class ReauthenticationRequired(Exception):
    """认证域已进入需人工/显式重新认证的状态。

    触发条件：refresh 被确定性拒绝，或 refresh 请求结果丢失（服务端是否
    完成轮换不可知）。旧 refresh 是一次性的，盲重试没有意义，也不允许
    伪造新 token，只能等待 reset() 注入新的外部凭据。
    """


class Auth:
    """共享 OAuth 客户端的认证轮换器。

    不变量：
    - 同一 refresh 代最多发起一次 rotate；同代 401 等待者共享同一次结果。
    - 迟到的旧 access 401 若发现自身代落后于当前代，直接返回当前 token，
      不再消费任何 refresh。
    - 失败/结果未知时认证域进入 reauth_required，同代等待者得到一致失败。
    - 新代只会单调前进，旧请求无法把状态回写倒退。
    """

    def __init__(self, source):
        self.source = source
        self.access = source.access
        self.refresh = source.refresh
        self.generation = 0
        self.reauth_required = False
        self._cond = threading.Condition()
        # 当前正在进行（或已完成）的单飞刷新所针对的 access 代
        self._in_flight_for = None
        self._in_flight_error = None

    def token(self):
        with self._cond:
            if self.reauth_required:
                raise ReauthenticationRequired(
                    f"auth domain stopped after {self._in_flight_error!r}; call reset()"
                )
            return self.access

    def snapshot(self):
        with self._cond:
            return {
                "access": self.access,
                "refresh": self.refresh,
                "generation": self.generation,
                "reauth_required": self.reauth_required,
                "in_flight_for": self._in_flight_for,
                "error": self._in_flight_error,
            }

    def refresh_after_401(self, used_token):
        """处理一次针对 used_token 的 401，返回可用于重放的 access token。"""
        with self._cond:
            if self.reauth_required:
                raise ReauthenticationRequired(
                    f"auth domain stopped after {self._in_flight_error!r}; call reset()"
                )
            # 迟到 401：新代已经存在，used_token 属于旧代。
            # 绝不再次消费新 refresh，直接共享当前认证结果。
            if used_token != self.access:
                if self.generation == 0 or not self._is_ancestor(used_token):
                    # 不属于本认证域已知代系的 token，拒绝处理
                    raise ReauthenticationRequired("unknown token lineage")
                return self.access
            if self._in_flight_for == used_token:
                # 同代已有单飞刷新，等待其结果
                self._cond.wait_for(lambda: self._in_flight_for != used_token)
                if self.reauth_required:
                    raise ReauthenticationRequired(
                        f"auth domain stopped after {self._in_flight_error!r}"
                    )
                if self._in_flight_error is not None:
                    raise ReauthenticationRequired(
                        f"leader rotation failed: {self._in_flight_error}"
                    )
                return self.access
            # 本代第一个遇到 401 的调用者，成为单飞 leader
            self._in_flight_for = used_token
            self._in_flight_error = None
            old_refresh = self.refresh

        try:
            new_access, new_refresh = self.source.rotate(old_refresh)
        except (InvalidRefresh, UnknownRotationOutcome) as exc:
            # 确定性拒绝或结果未知：整个认证域一致停止，不盲重试、不伪造。
            with self._cond:
                self.reauth_required = True
                self._in_flight_error = f"{type(exc).__name__}: {exc}"
                self._in_flight_for = None
                self._cond.notify_all()
            raise ReauthenticationRequired(
                f"{type(exc).__name__}: {exc}; refresh outcome unusable"
            )
        except Exception as exc:
            # 非预期故障也要解除单飞并唤醒等待者，禁止泄漏为永久挂起
            with self._cond:
                self._in_flight_error = f"{type(exc).__name__}: {exc}"
                self._in_flight_for = None
                self._cond.notify_all()
            raise
        with self._cond:
            # 单调前进：仅当仍是发起时的代才提交
            if self._in_flight_for != used_token or self.reauth_required:
                self._in_flight_for = None
                self._cond.notify_all()
                raise ReauthenticationRequired("auth state moved during rotation")
            self.access = new_access
            self.refresh = new_refresh
            self.generation += 1
            self._in_flight_for = None
            self._cond.notify_all()
            return new_access

    def _is_ancestor(self, token):
        """used_token 是否属于本认证域历史代系（a0/a1/...）。"""
        if not (isinstance(token, str) and token.startswith("a")):
            return False
        try:
            gen = int(token[1:])
        except ValueError:
            return False
        return 0 <= gen <= self.generation

    def reset(self, access=None, refresh=None):
        """显式重新认证：外部流程取得新凭据后注入，认证域恢复工作。"""
        with self._cond:
            self.access = access if access is not None else self.source.access
            self.refresh = refresh if refresh is not None else self.source.refresh
            self.reauth_required = False
            self._in_flight_for = None
            self._in_flight_error = None
            self.generation = self.source.generation
            self._cond.notify_all()
            return self.access
