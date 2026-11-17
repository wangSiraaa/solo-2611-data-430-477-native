"""小型 decode 入口：用真实历史记录证明兼容结论。

只处理 JSON 式 Python 值（不实现 Avro 二进制编码、union、alias、logical type）。
decode(reader_schema, value) 按 *reader* 的展开模式解释 writer 写出的值：
  - 标量按规则读取，writer int -> reader long 提升并在 trace 中留痕
  - null 只有 null 类型接纳；字段缺失 != 显式 null：
      缺失字段走默认值注入（default_injected 事件），显式 null 必须由 null 类型接纳
  - array 逐项递归校验（含默认数组的逐项校验）
  - record 按字段名匹配，writer 多余字段忽略（field_ignored 事件）
另外提供 replay_history：用某 subject 的真实历史版本互读同一份载荷，
输出每一对 reader/writer 的解码结果或失败原因，作为兼容性的行为级证据。
"""
from resolver import normalize


class DecodeError(ValueError):
    def __init__(self, message, path="$"):
        super().__init__(f"{path}: {message}")
        self.path = path


def _check_scalar(t, value, path):
    if t == "null":
        if value is not None:
            raise DecodeError(f"null type only accepts null, got {value!r}", path)
    elif t == "int":
        if isinstance(value, bool) or not isinstance(value, int) or not (-(2**31) <= value <= 2**31 - 1):
            raise DecodeError(f"int32 expected, got {value!r}", path)
    elif t == "long":
        if isinstance(value, bool) or not isinstance(value, int) or not (-(2**63) <= value <= 2**63 - 1):
            raise DecodeError(f"int64 expected, got {value!r}", path)
    elif t == "string":
        if not isinstance(value, str):
            raise DecodeError(f"string expected, got {value!r}", path)


def decode(schema, value, path="$", trace=None):
    """按展开后的 reader schema 解码一个 JSON 式值，返回解释后的规范化值。"""
    schema = normalize(schema) if "type" not in schema else schema
    if trace is None:
        trace = []
    t = schema["type"]

    if t in ("null", "int", "long", "string"):
        _check_scalar(t, value, path)
        if t == "long" and isinstance(value, int) and not isinstance(value, bool) \
                and -(2**31) <= value <= 2**31 - 1:
            trace.append({"event": "int_promoted_to_long", "path": path, "value": value})
        return value

    if t == "array":
        if not isinstance(value, list):
            raise DecodeError(f"array expected, got {type(value).__name__}", path)
        return [decode(schema["items"], item, f"{path}[{i}]", trace) for i, item in enumerate(value)]

    if t == "record":
        if not isinstance(value, dict):
            raise DecodeError(f"record expected, got {type(value).__name__}", path)
        out = {}
        reader_names = set()
        for f in schema["fields"]:
            sub = f"{path}.{f['name']}"
            reader_names.add(f["name"])
            if f["name"] not in value:
                if "default" not in f:
                    raise DecodeError(
                        f"field {f['name']!r} missing and reader declares no default", path
                    )
                # 默认值按 reader 声明类型逐项校验后注入（缺失 ≠ 显式 null）
                injected = decode(f["schema"], _deep_copy(f["default"]), sub, trace)
                trace.append({"event": "default_injected", "path": sub, "value": injected})
                out[f["name"]] = injected
            else:
                out[f["name"]] = decode(f["schema"], value[f["name"]], sub, trace)
        for extra in set(value) - reader_names:
            trace.append({"event": "field_ignored", "path": f"{path}.{extra}"})
        return out

    raise DecodeError(f"unresolved or unsupported type {t!r}", path)


def decode_with_trace(schema, value):
    trace = []
    result = decode(schema, value, trace=trace)
    return result, trace


def _deep_copy(value):
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value


# ---------------------------------------------------------------- 历史回放
def replay_history(registry, subject, writer_version, payload):
    """用 subject 的真实发布历史回放一条按 writer_version 写出的记录。

    每一个历史版本依次作为 reader 尝试读取该 payload：
      - can_read 判定为可读时，实际执行 decode，给出解释结果与默认注入/字段忽略轨迹
      - 不可读时，给出 reader/writer 版本与具体原因，不做解码
    返回行为级证据，而不是一组布尔值。
    """
    from compatibility import can_read

    versions = registry.catalog.all_versions(subject)
    expanded = {v: registry.resolve(subject, v) for v in versions}
    writer_schema = expanded[writer_version]
    report = []
    for reader_v in versions:
        reader_schema = expanded[reader_v]
        ok, reason = can_read(reader_schema, writer_schema)
        entry = {
            "reader": f"v{reader_v}",
            "writer": f"v{writer_version}",
            "compatible": ok,
            "reason": reason,
        }
        if ok:
            try:
                result, trace = decode_with_trace(reader_schema, payload)
                entry["decoded"] = result
                entry["trace"] = trace
            except DecodeError as exc:
                entry["compatible"] = False
                entry["reason"] = f"decode failed: {exc}"
        report.append(entry)
    return report
