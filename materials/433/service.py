import hashlib
import json
import threading
import time


class Unauthorized(Exception):
    pass


class InvalidRefresh(Exception):
    pass


class UnknownRotationOutcome(Exception):
    """refresh 请求已发出，但响应丢失，服务端是否完成轮换不可知。"""


def page_digest(page_id, rows, nxt):
    """源适配器与采集端共享的稳定页摘要，覆盖 page_id / rows / next 整体。"""
    body = json.dumps(
        {"page_id": page_id, "rows": rows, "next": nxt},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class Source:
    """本地确定性替身。get/rotate 接口保持不变，额外控制点默认关闭。"""

    def __init__(self):
        self.access = "a0"
        self.refresh = "r0"
        self.generation = 0
        self.lock = threading.Lock()
        self.refresh_calls = 0
        # 真实 refresh 调用日志：每次 rotate 尝试一条，含所用 refresh 与结果
        self.refresh_log = []
        # 每次通过鉴权的 GET 记录
        self.get_log = []
        # 控制点
        self.get_delay = 0.0
        self.truncate_once = set()          # {(stream, page)} 首次 GET 返回截断前缀
        self.force_401_once = set()         # {(stream, page)} 下一次 GET 强制 401
        self.replay_once = {}               # {(stream, page): (base_page, variant_rows)}
        self.reject_refresh = False         # rotate 确定性拒绝
        self.lost_refresh = False           # rotate 发出后结果丢失
        self.lost_after_server_rotate = True
        self.pages = {
            "A": [["a1", "a2"], ["a3"], ["a4", "a5"], ["a6"]],
            "B": [["b1"], ["b2", "b3"], ["b4"], ["b5", "b6"]],
            "C": [["c1", "c2"], ["c3", "c4"], ["c5"], ["c6", "c7", "c8"]],
        }

    def _full_page(self, stream, page):
        rows = self.pages[stream][page]
        page_id = f"{stream}:{page}"
        nxt = page + 1 if page + 1 < len(self.pages[stream]) else None
        return {
            "page_id": page_id,
            "rows": rows,
            "complete": True,
            "next": nxt,
            "digest": page_digest(page_id, rows, nxt),
        }

    def get(self, stream, page, token, expired=False):
        if self.get_delay:
            time.sleep(self.get_delay)
        if expired or token != self.access:
            raise Unauthorized(token)
        forced = (stream, page)
        if forced in self.force_401_once:
            self.force_401_once.discard(forced)
            raise Unauthorized(token)
        self.get_log.append((stream, page, token))

        replay = self.replay_once.pop(forced, None)
        if replay is not None:
            base_page, variant_rows = replay
            base = self._full_page(stream, base_page)
            if variant_rows is not None:
                base["rows"] = variant_rows
                base["digest"] = page_digest(base["page_id"], variant_rows, base["next"])
            return base

        if forced in self.truncate_once:
            self.truncate_once.discard(forced)
            full = self._full_page(stream, page)
            prefix = full["rows"][:1]
            return {
                "page_id": full["page_id"],
                "rows": prefix,
                "complete": False,
                "next": None,
                "digest": full["digest"],
            }
        return self._full_page(stream, page)

    def rotate(self, refresh):
        with self.lock:
            self.refresh_calls += 1
            entry = {"call": self.refresh_calls, "used": refresh}
            if refresh != self.refresh:
                entry["outcome"] = "invalid"
                self.refresh_log.append(entry)
                raise InvalidRefresh(refresh)
            if self.reject_refresh:
                entry["outcome"] = "invalid"
                self.refresh_log.append(entry)
                raise InvalidRefresh(refresh)
            if self.lost_refresh:
                # 结果丢失：服务端可能已经销毁旧 refresh，客户端无法获知
                if self.lost_after_server_rotate:
                    self.generation += 1
                    self.access = f"a{self.generation}"
                    self.refresh = f"r{self.generation}"
                    entry["generation_after"] = self.generation
                entry["outcome"] = "lost"
                self.refresh_log.append(entry)
                raise UnknownRotationOutcome(refresh)
            self.generation += 1
            self.access = f"a{self.generation}"
            self.refresh = f"r{self.generation}"
            entry["outcome"] = "ok"
            entry["generation_after"] = self.generation
            self.refresh_log.append(entry)
            return self.access, self.refresh
