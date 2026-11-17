"""模式规范化、引用闭包构建与按锁定版本展开。

规则（仅此这些，无 union/alias/logical type）：
  类型：int / long / string / null / array(items) / record(name,fields) / ref(subject,version)
  - ref 必须指向已发布的精确版本；缺失或循环 => 整次注册失败
  - 引用链深度上限 8
  - record 的 name 必须在整个引用闭包内唯一（同一 (subject,version) 来源的重复出现不算冲突，
    那是同一个类型被多次引用；两个不同来源给出同名 record 才是冲突）
  - 字段名在单个 record 内唯一；reader 新增字段的 default 必须与其类型逐项合法
"""

SCALARS = {"int", "long", "string", "null"}
ALL_PRIMITIVES = SCALARS
MAX_REF_DEPTH = 8
MAX_SUBJECTS = 10
MAX_VERSIONS_PER_SUBJECT = 20


class SchemaError(ValueError):
    """模式本身不合法（形状、未知类型、默认值、闭包、循环、深度、重名等）。"""


# ---------------------------------------------------------------- 规范化
def normalize(schema):
    """接受裸类型字符串（'int'）或字典形式，统一成字典，并做严格形状校验。"""
    if isinstance(schema, str):
        if schema not in ALL_PRIMITIVES:
            raise SchemaError(f"unknown primitive type: {schema!r}")
        return {"type": schema}
    if not isinstance(schema, dict) or "type" not in schema:
        raise SchemaError(f"schema must be a type string or object with 'type': {schema!r}")
    t = schema["type"]
    if t in SCALARS:
        if set(schema) != {"type"}:
            raise SchemaError(f"{t} schema must not carry extra keys: {sorted(set(schema)-{'type'})}")
        return {"type": t}
    if t == "array":
        if set(schema) != {"type", "items"}:
            raise SchemaError("array schema requires exactly type+items")
        return {"type": "array", "items": normalize(schema["items"])}
    if t == "record":
        if set(schema) - {"type", "name", "fields"}:
            raise SchemaError("record schema only allows type/name/fields")
        if not isinstance(schema.get("name"), str) or not schema["name"]:
            raise SchemaError("record requires non-empty string name")
        fields = schema.get("fields")
        if not isinstance(fields, list) or not fields:
            raise SchemaError(f"record {schema['name']!r} requires a non-empty fields list")
        seen = set()
        norm_fields = []
        for f in fields:
            if not isinstance(f, dict) or "name" not in f or "schema" not in f:
                raise SchemaError(f"record {schema['name']!r}: each field needs name+schema")
            name = f["name"]
            if not isinstance(name, str) or not name:
                raise SchemaError(f"record {schema['name']!r}: field name must be non-empty string")
            if name in seen:
                raise SchemaError(f"record {schema['name']!r}: duplicate field name {name!r}")
            seen.add(name)
            entry = {"name": name, "schema": normalize(f["schema"])}
            if "default" in f:
                entry["default"] = f["default"]  # 合法性在 validate_default 中递归校验
            if set(f) - {"name", "schema", "default"}:
                raise SchemaError(f"field {name!r} only allows name/schema/default")
            norm_fields.append(entry)
        return {"type": "record", "name": schema["name"], "fields": norm_fields}
    if t == "ref":
        if set(schema) != {"type", "subject", "version"}:
            raise SchemaError("ref schema requires exactly type+subject+version")
        subject, version = schema["subject"], schema["version"]
        if not isinstance(subject, str) or not subject:
            raise SchemaError("ref subject must be non-empty string")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise SchemaError(f"ref version must be a positive integer, got {version!r}")
        return {"type": "ref", "subject": subject, "version": version}
    raise SchemaError(f"unknown schema type: {t!r}")


# ---------------------------------------------------------------- 默认值
def validate_default(schema, value, at="$"):
    """默认值必须是该类型的合法 JSON 式值；array 逐项校验，record 逐字段校验。"""
    t = schema["type"]
    if t == "null":
        if value is not None:
            raise SchemaError(f"default at {at}: null type only accepts null, got {value!r}")
    elif t == "int":
        if isinstance(value, bool) or not isinstance(value, int) or not (-(2**31) <= value <= 2**31 - 1):
            raise SchemaError(f"default at {at}: int32 expected, got {value!r}")
    elif t == "long":
        if isinstance(value, bool) or not isinstance(value, int) or not (-(2**63) <= value <= 2**63 - 1):
            raise SchemaError(f"default at {at}: int64 expected, got {value!r}")
    elif t == "string":
        if not isinstance(value, str):
            raise SchemaError(f"default at {at}: string expected, got {value!r}")
    elif t == "array":
        if not isinstance(value, list):
            raise SchemaError(f"default at {at}: array expected, got {value!r}")
        for i, item in enumerate(value):
            validate_default(schema["items"], item, f"{at}[{i}]")
    elif t == "record":
        if not isinstance(value, dict):
            raise SchemaError(f"default at {at}: record expected, got {value!r}")
        by_name = {f["name"]: f for f in schema["fields"]}
        if set(value) - set(by_name):
            raise SchemaError(f"default at {at}: unknown fields {sorted(set(value)-set(by_name))}")
        for name, f in by_name.items():
            if name not in value:
                raise SchemaError(f"default at {at}: record field {name!r} missing")
            validate_default(f["schema"], value[name], f"{at}.{name}")
    else:  # pragma: no cover - ref 在此之前应已展开
        raise SchemaError(f"default at {at}: cannot default to unresolved ref {t}")


def validate_all_defaults(schema, at="$"):
    """对规范化（未展开）模式里所有带 default 的字段做合法性校验。"""
    t = schema["type"]
    if t == "array":
        validate_all_defaults(schema["items"], at + "[]")
    elif t == "record":
        for f in schema["fields"]:
            if "default" in f:
                if f["schema"]["type"] == "ref":
                    raise SchemaError(
                        f"default at {at}.{f['name']}: default cannot be declared on a ref; "
                        "restate the resolved type instead"
                    )
                validate_default(f["schema"], f["default"], f"{at}.{f['name']}")
            validate_all_defaults(f["schema"], f"{at}.{f['name']}")


# ---------------------------------------------------------------- 闭包
class Resolver:
    def __init__(self, catalog):
        self.catalog = catalog

    def build_closure(self, root_schema):
        """遍历规范化后的根模式，解析全部 ref。

        返回 (closure, env)：
          closure —— 排序去重后的 [(subject, version), ...]（含传递依赖，不含根自身）
          env     —— {(subject, version): 规范化目标 body}，供 expand 使用
        失败（缺版本/循环/超深/record 重名）一律抛 SchemaError，整次注册作废。
        """
        closure = set()
        env = {}
        record_origins = {}  # record name -> (subject, version) 来源；根模式来源记为 ("$root", 0)

        def fetch(subject, version):
            key = (subject, version)
            if key in env:
                return env[key]
            try:
                contract = self.catalog.get_contract(subject, version)
            except KeyError:
                raise SchemaError(f"ref target not published: {subject} v{version}")
            env[key] = contract["body"]
            closure.add(key)
            return contract["body"]

        def walk(schema, depth, origin, stack):
            """stack 记录当前已进入的契约键（含根 ("$root",0)），回边即循环。"""
            t = schema["type"]
            if t == "ref":
                if depth >= MAX_REF_DEPTH:
                    raise SchemaError(
                        f"ref depth exceeds {MAX_REF_DEPTH} at {schema['subject']} v{schema['version']} "
                        f"(chain: {' -> '.join(f'{s} v{v}' for s, v in stack)})"
                    )
                key = (schema["subject"], schema["version"])
                if key in stack:
                    cycle = " -> ".join(f"{s} v{v}" for s, v in stack + (key,))
                    raise SchemaError(f"cyclic ref detected: {cycle}")
                target = fetch(*key)
                walk(target, depth + 1, key, stack + (key,))
            elif t == "array":
                walk(schema["items"], depth, origin, stack)
            elif t == "record":
                prior = record_origins.get(schema["name"])
                if prior is not None and prior != origin:
                    raise SchemaError(
                        f"record name {schema['name']!r} is not unique in closure: "
                        f"defined by {prior[0]} v{prior[1]} and {origin[0]} v{origin[1]}"
                    )
                record_origins[schema["name"]] = origin
                for f in schema["fields"]:
                    walk(f["schema"], depth, origin, stack)

        walk(root_schema, 0, ("$root", 0), (("$root", 0),))
        return sorted(closure), env

    def expand(self, schema, env=None):
        """把模式（或已发布契约）展开成全内联的可解析模式，ref 严格按锁定版本解析。

        env 为 None 时按 schema 中出现的每个 ref 自行从 catalog 取精确版本；
        读取已发布契约时应传入契约闭包构建的 env，保证解释与发布时逐字节一致，
        后来发布的 latest 不会嵌入旧契约。
        """
        if env is None:
            env = {}

        def resolve_ref(ref):
            key = (ref["subject"], ref["version"])
            if key not in env:
                try:
                    env[key] = self.catalog.get_contract(*key)["body"]
                except KeyError:
                    raise SchemaError(f"ref target not published: {ref['subject']} v{ref['version']}")
            return env[key]

        def go(node, stack):
            t = node["type"]
            if t == "ref":
                key = (node["subject"], node["version"])
                if key in stack:
                    raise SchemaError(
                        "cyclic ref during expand: " +
                        " -> ".join(f"{s} v{v}" for s, v in stack + (key,))
                    )
                return go(resolve_ref(node), stack + (key,))
            if t == "array":
                return {"type": "array", "items": go(node["items"], stack)}
            if t == "record":
                return {
                    "type": "record",
                    "name": node["name"],
                    "fields": [
                        {**f, "schema": go(f["schema"], stack)} for f in node["fields"]
                    ],
                }
            return dict(node)

        return go(normalize(schema), ())

    def expand_contract(self, contract):
        """用发布时锁定的闭包展开契约，get() 路径的确定性解释。"""
        env = {}
        for subject, version in contract["closure"]:
            env[(subject, version)] = self.catalog.get_contract(subject, version)["body"]
        return self.expand(contract["body"], env)
