"""递归兼容性检查与注册流程。

reader 能读 writer 的全部规则（仅这些）：
  - 同标量类型直接可读；writer int 可提升为 reader long（唯一的类型提升，反向不可）
  - null 只有 null 类型接纳；字段缺失与显式 null 是两回事
  - array：递归要求 items 可读
  - record：同 name 按字段名匹配
      reader 新增字段必须带合法类型默认值；writer 多余字段忽略；
      同名字段的 schema 必须递归可读
  - 其余组合一律不兼容

模式：
  BACKWARD / BACKWARD_TRANSITIVE   新 schema 作 reader，读历史 writer
  FORWARD  / FORWARD_TRANSITIVE    新 schema 作 writer，被历史 reader 读
  FULL     / FULL_TRANSITIVE       两个方向都成立
  NONE                             不检查
TRANSITIVE 覆盖该 subject 的每一个历史版本，不允许只和上一个版本比、跳过中间版本。
"""
import sqlite3
import time

from catalog import digest_of
from resolver import (
    MAX_SUBJECTS,
    MAX_VERSIONS_PER_SUBJECT,
    SchemaError,
    normalize,
    validate_all_defaults,
    validate_default,
)


class Incompatible(ValueError):
    """兼容性检查失败；failures 携带每个失败路径的 reader/writer 版本与原因。"""

    def __init__(self, message, failures=None):
        super().__init__(message)
        self.failures = failures or []


# ---------------------------------------------------------------- 递归可读
def can_read(reader, writer, path="$"):
    """返回 (ok, reason)。reader/writer 均为已展开模式。"""
    rt, wt = reader["type"], writer["type"]

    if rt == "null" or wt == "null":
        # null 只有 null 接纳；int/long/... 读不了显式 null，null 也读不了别的
        if rt == wt == "null":
            return True, None
        return False, f"{path}: null is only accepted by null (reader={rt}, writer={wt})"

    if rt in ("int", "long", "string") or wt in ("int", "long", "string"):
        if rt == wt:
            return True, None
        if rt == "long" and wt == "int":  # 唯一允许的提升
            return True, None
        return False, f"{path}: scalar mismatch reader={rt} writer={wt} (only int->long promotion allowed)"

    if rt == "array" and wt == "array":
        return can_read(reader["items"], writer["items"], path + "[]")

    if rt == "record" and wt == "record":
        if reader["name"] != writer["name"]:
            return False, f"{path}: record name mismatch reader={reader['name']!r} writer={writer['name']!r}"
        wfields = {f["name"]: f for f in writer["fields"]}
        for rf in reader["fields"]:
            sub = f"{path}.{rf['name']}"
            if rf["name"] not in wfields:
                if "default" not in rf:
                    return False, f"{sub}: reader-added field has no default"
                # 默认值在注册阶段已按类型校验；这里防御性再确认一次
                try:
                    validate_default(rf["schema"], rf["default"], sub)
                except SchemaError as exc:
                    return False, f"{sub}: invalid default: {exc}"
                continue
            ok, reason = can_read(rf["schema"], wfields[rf["name"]]["schema"], sub)
            if not ok:
                return False, reason
        # writer 多出的字段：reader 直接忽略
        return True, None

    if rt == "array" or wt == "array":
        return False, f"{path}: array vs non-array (reader={rt}, writer={wt})"
    return False, f"{path}: incompatible types reader={rt} writer={wt}"


# ---------------------------------------------------------------- 注册
class Registry:
    def __init__(self, catalog, resolver):
        self.catalog = catalog
        self.resolver = resolver

    def _expanded_history(self, subject):
        """按版本顺序返回该 subject 全部历史版本的展开模式（基于各自锁定闭包）。"""
        out = []
        for v in self.catalog.all_versions(subject):
            contract = self.catalog.get_contract(subject, v)
            out.append((v, self.resolver.expand_contract(contract)))
        return out

    def _check_mode(self, mode, candidate_expanded, history):
        """history: [(version, expanded), ...]。返回失败明细列表。"""
        failures = []
        check_backward = mode in ("BACKWARD", "BACKWARD_TRANSITIVE", "FULL", "FULL_TRANSITIVE")
        check_forward = mode in ("FORWARD", "FORWARD_TRANSITIVE", "FULL", "FULL_TRANSITIVE")
        transitive = "TRANSITIVE" in mode

        # 非传递模式只看最新版本；传递模式必须覆盖每一个历史 writer/reader（含中间版本）
        targets = history if transitive else history[-1:]

        for old_version, old_expanded in targets:
            if check_backward:
                ok, reason = can_read(candidate_expanded, old_expanded)
                if not ok:
                    failures.append({
                        "direction": "backward",
                        "reader": "candidate",
                        "writer": f"v{old_version}",
                        "reason": reason,
                    })
            if check_forward:
                ok, reason = can_read(old_expanded, candidate_expanded)
                if not ok:
                    failures.append({
                        "direction": "forward",
                        "reader": f"v{old_version}",
                        "writer": "candidate",
                        "reason": reason,
                    })
        return failures

    def register(self, subject, schema, mode="BACKWARD_TRANSITIVE"):
        if mode not in (
            "BACKWARD", "BACKWARD_TRANSITIVE",
            "FORWARD", "FORWARD_TRANSITIVE",
            "FULL", "FULL_TRANSITIVE",
            "NONE",
        ):
            raise ValueError(f"unknown compatibility mode: {mode}")

        # 1) 规范化 + 本模式内默认值合法性（失败不产生任何写入，也没有成功回执）
        norm = self._normalize(schema)

        # 2) 事务边界：闭包解析、历史读取、兼容检查、版本分配、发布全部在同一写事务内。
        #    BEGIN IMMEDIATE 保证两个并发注册不可能同时基于同一份过时历史通过。
        last_error = None
        for attempt in range(50):
            try:
                with self.catalog.write_txn():
                    return self._register_locked(subject, norm, mode)
            except sqlite3.OperationalError as exc:  # 极端竞争下的 SQLITE_BUSY，退避后整流程重试
                last_error = exc
                time.sleep(0.02 * (attempt + 1))
        raise RuntimeError("register retried 50 times under database lock contention") from last_error

    def _normalize(self, schema):
        norm = normalize(schema)
        validate_all_defaults(norm)
        return norm

    def _register_locked(self, subject, norm, mode):
        # 2a) 引用闭包：精确版本、循环、深度、record 闭包内重名全部在此拦截
        closure, env = self.resolver.build_closure(norm)

        # 展开候选模式（复用闭包 env），默认值针对展开后的真实类型再递归校验一遍，
        # 使“reader 新增字段默认值合法”在 ref 指向的类型上也成立
        expanded = self.resolver.expand(norm, dict(env))
        validate_all_defaults(expanded)

        # 2b) 容量上限
        exists = self.catalog.subject_exists(subject)
        if not exists and self.catalog.count_subjects() >= MAX_SUBJECTS:
            raise SchemaError(f"subject limit {MAX_SUBJECTS} reached; refusing new subject {subject!r}")
        if exists and self.catalog.count_versions(subject) >= MAX_VERSIONS_PER_SUBJECT:
            raise SchemaError(
                f"subject {subject!r} already has {MAX_VERSIONS_PER_SUBJECT} versions"
            )

        # 2c) 传递兼容性：与每一个历史版本比较（NONE 跳过）。
        # 必须先于幂等返回：重试只是“不新增重复”，不能把一次按当前模式本不该通过的
        # 注册伪装成成功回执。
        if mode != "NONE":
            history = self._expanded_history(subject)
            failures = self._check_mode(mode, expanded, history)
            if failures:
                head = failures[0]
                raise Incompatible(
                    f"{mode} check failed: {len(failures)} incompatible pair(s), "
                    f"first: [{head['direction']}] {head['reader']} vs {head['writer']} — {head['reason']}",
                    failures,
                )

        # 2d) 幂等：同规范正文 + 同闭包 => 同一摘要 => 返回原版本，不新增重复
        digest = digest_of(norm, closure)
        existing = self.catalog.find_by_digest(subject, digest)
        if existing is not None:
            return {"version": existing, "duplicate": True, "digest": digest}

        # 2e) 版本分配与发布（仍在同一事务内）
        next_version = (self.catalog.latest_version(subject) or 0) + 1
        self.catalog.put(subject, next_version, norm, closure, digest)
        return {"version": next_version, "duplicate": False, "digest": digest}

    def get(self, subject, version):
        """始终返回同一可解析契约：不可变正文 + 发布时锁定的闭包 + 摘要。"""
        return self.catalog.get_contract(subject, version)

    def resolve(self, subject, version):
        """返回按锁定闭包展开的模式；后来的 latest 发布不会改变这里的解释。"""
        return self.resolver.expand_contract(self.get(subject, version))
