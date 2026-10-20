"""ARM template expression parser and evaluator.

Values that cannot be known without a real deployment (``reference()`` of a live resource,
``listKeys()``, secrets, missing parameters) evaluate to :class:`Unknown`. Unknown values
propagate through functions that need to inspect them, but stay opaque inside objects and
arrays so that the rest of a structure remains readable.
"""

import base64
import hashlib
import json
import re
import urllib.parse
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeAlias


@dataclass(frozen=True)
class Unknown:
    reason: str = ""

    def __repr__(self) -> str:
        return f"Unknown({self.reason!r})" if self.reason else "Unknown()"


JsonValue: TypeAlias = "str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | Unknown | None"


def is_unknown(value: object) -> bool:
    return isinstance(value, Unknown)


class ArmError(Exception):
    pass


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Lit:
    value: str | int | float


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple["Node", ...]


@dataclass(frozen=True)
class Prop:
    target: "Node"
    name: str


@dataclass(frozen=True)
class Index:
    target: "Node"
    index: "Node"


Node: TypeAlias = Lit | Call | Prop | Index

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<str>'(?:[^']|'')*')
      | (?P<num>-?\d+(?:\.\d+)?)
      | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<punct>[()\[\],.])
    )""",
    re.VERBOSE,
)


def _tokenize(text: str) -> list[tuple[str, str]]:
    pos = 0
    out: list[tuple[str, str]] = []
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        m = _TOKEN.match(text, pos)
        if not m:
            raise ArmError(f"cannot tokenize expression at {pos}: {text!r}")
        kind = m.lastgroup or ""
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]], source: str) -> None:
        self.tokens = tokens
        self.pos = 0
        self.source = source

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _next(self) -> tuple[str, str]:
        tok = self._peek()
        if tok is None:
            raise ArmError(f"unexpected end of expression: {self.source!r}")
        self.pos += 1
        return tok

    def _expect(self, value: str) -> None:
        kind, text = self._next()
        if kind != "punct" or text != value:
            raise ArmError(f"expected {value!r}, got {text!r} in {self.source!r}")

    def parse(self) -> Node:
        node = self._expr()
        if self._peek() is not None:
            raise ArmError(f"trailing tokens in expression {self.source!r}")
        return node

    def _expr(self) -> Node:
        kind, text = self._next()
        node: Node
        if kind == "str":
            node = Lit(text[1:-1].replace("''", "'"))
        elif kind == "num":
            node = Lit(float(text) if "." in text else int(text))
        elif kind == "ident":
            nxt = self._peek()
            if nxt == ("punct", "("):
                self._next()
                args: list[Node] = []
                if self._peek() == ("punct", ")"):
                    self._next()
                else:
                    while True:
                        args.append(self._expr())
                        _, sep = self._next()
                        if sep == ")":
                            break
                        if sep != ",":
                            raise ArmError(f"expected ',' or ')' in {self.source!r}")
                node = Call(text, tuple(args))
            elif text in ("true", "false", "null"):
                node = Call(text, ())
            else:
                raise ArmError(f"bare identifier {text!r} in {self.source!r}")
        else:
            raise ArmError(f"unexpected token {text!r} in {self.source!r}")
        while True:
            tok = self._peek()
            if tok == ("punct", "."):
                self._next()
                k, name = self._next()
                if k != "ident":
                    raise ArmError(f"bad property name in {self.source!r}")
                node = Prop(node, name)
            elif tok == ("punct", "["):
                self._next()
                idx = self._expr()
                self._expect("]")
                node = Index(node, idx)
            else:
                return node


_parse_cache: dict[str, Node] = {}


def parse_expression(text: str) -> Node:
    """Parse the inside of an ARM ``[...]`` expression."""
    cached = _parse_cache.get(text)
    if cached is None:
        cached = _Parser(_tokenize(text), text).parse()
        _parse_cache[text] = cached
    return cached


def is_expression(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) >= 2
        and value.startswith("[")
        and value.endswith("]")
        and not value.startswith("[[")
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


class EvalContext(Protocol):
    """What the function library needs from the template engine."""

    def get_parameter(self, name: str) -> JsonValue: ...

    def get_variable(self, name: str) -> JsonValue: ...

    def copy_index(self, loop_name: str | None) -> int: ...

    def reference(self, args: Sequence[JsonValue]) -> JsonValue: ...

    def resource_group(self) -> JsonValue: ...

    def subscription(self) -> JsonValue: ...

    def deployment(self) -> JsonValue: ...

    @property
    def default_subscription_id(self) -> str: ...

    @property
    def default_resource_group(self) -> str: ...


def evaluate(node: Node, ctx: EvalContext) -> JsonValue:
    if isinstance(node, Lit):
        return node.value
    if isinstance(node, Prop):
        return get_member(evaluate(node.target, ctx), node.name)
    if isinstance(node, Index):
        return get_member(evaluate(node.target, ctx), evaluate(node.index, ctx))
    return _call(node, ctx)


def evaluate_string(text: str, ctx: EvalContext) -> JsonValue:
    if not is_expression(text):
        return text[1:] if text.startswith("[[") else text
    try:
        return evaluate(parse_expression(text[1:-1]), ctx)
    except ArmError as exc:
        return Unknown(str(exc))


def get_member(target: JsonValue, key: JsonValue) -> JsonValue:
    if isinstance(target, Unknown):
        return target
    if isinstance(key, Unknown):
        return key
    if isinstance(target, dict):
        if not isinstance(key, str):
            return Unknown(f"non-string key {key!r}")
        if key in target:
            return target[key]
        lowered = key.lower()
        for k, v in target.items():
            if k.lower() == lowered:
                return v
        return Unknown(f"missing property {key}")
    if isinstance(target, list):
        if isinstance(key, int) and not isinstance(key, bool) and -len(target) <= key < len(target):
            return target[key]
        return Unknown(f"bad index {key!r}")
    return Unknown(f"cannot read {key!r} of {type(target).__name__}")


_LAZY = {"if", "and", "or", "coalesce"}


def _call(node: Call, ctx: EvalContext) -> JsonValue:
    name = node.name.lower()
    if name in _LAZY:
        return _lazy_call(name, node.args, ctx)
    args = [evaluate(a, ctx) for a in node.args]
    return call_function(name, args, ctx)


def _lazy_call(name: str, arg_nodes: Sequence[Node], ctx: EvalContext) -> JsonValue:
    if name == "if":
        cond = evaluate(arg_nodes[0], ctx)
        if isinstance(cond, Unknown):
            return cond
        return evaluate(arg_nodes[1] if _truthy(cond) else arg_nodes[2], ctx)
    if name == "and":
        saw_unknown = False
        for a in arg_nodes:
            v = evaluate(a, ctx)
            if isinstance(v, Unknown):
                saw_unknown = True
            elif not _truthy(v):
                return False
        return Unknown("and") if saw_unknown else True
    if name == "or":
        saw_unknown = False
        for a in arg_nodes:
            v = evaluate(a, ctx)
            if isinstance(v, Unknown):
                saw_unknown = True
            elif _truthy(v):
                return True
        return Unknown("or") if saw_unknown else False
    # coalesce: first non-null (Unknown counts as "can't tell")
    for a in arg_nodes:
        v = evaluate(a, ctx)
        if isinstance(v, Unknown):
            return v
        if v is not None:
            return v
    return None


def _truthy(v: JsonValue) -> bool:
    return bool(v)


# -- function library --------------------------------------------------------

_Fn: TypeAlias = Callable[[list[JsonValue], EvalContext], JsonValue]

# Functions that must see Unknown arguments themselves (they keep Unknown opaque).
_OPAQUE_OK = {
    "createobject",
    "createarray",
    "array",
    "concat",
    "union",
    "reference",
    "resourceid",
    "subscriptionresourceid",
    "extensionresourceid",
    "tenantresourceid",
    "format",
    "tryget",
    "first",
    "last",
    "take",
    "skip",
    "shallowmerge",
}


def call_function(name: str, args: list[JsonValue], ctx: EvalContext) -> JsonValue:
    fn = _FUNCTIONS.get(name)
    if fn is None:
        if name.startswith("list"):
            return Unknown(f"{name}() needs a live resource")
        return Unknown(f"unsupported function {name}()")
    if name not in _OPAQUE_OK:
        for a in args:
            if isinstance(a, Unknown):
                return a
    try:
        return fn(args, ctx)
    except (ArmError, TypeError, ValueError, IndexError, KeyError, ZeroDivisionError) as exc:
        return Unknown(f"{name}(): {exc}")


def _s(v: JsonValue) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(v, separators=(",", ":"), default=lambda o: None)


def _fn_concat(args: list[JsonValue], _: EvalContext) -> JsonValue:
    if any(isinstance(a, Unknown) for a in args):
        if any(isinstance(a, list) for a in args):
            # An unknown operand next to arrays stays in the result as one opaque element.
            out: list[JsonValue] = []
            for a in args:
                if isinstance(a, list):
                    out.extend(a)
                else:
                    out.append(a)
            return out
        return Unknown("concat with unknown")
    lists = [a for a in args if isinstance(a, list)]
    if args and len(lists) == len(args):
        return [item for lst in lists for item in lst]
    return "".join(_s(a) for a in args)


_FORMAT_RE = re.compile(r"\{\{|\}\}|\{(\d+)(?::([^}]*))?\}")


def _fn_format(args: list[JsonValue], _: EvalContext) -> JsonValue:
    fmt = args[0]
    if isinstance(fmt, Unknown):
        return fmt
    values = args[1:]
    unknown = False

    def sub(m: re.Match[str]) -> str:
        nonlocal unknown
        if m.group(0) == "{{":
            return "{"
        if m.group(0) == "}}":
            return "}"
        v = values[int(m.group(1))]
        if isinstance(v, Unknown):
            unknown = True
            return ""
        spec = m.group(2)
        if spec and isinstance(v, (int, float)) and not isinstance(v, bool):
            return _format_number(v, spec)
        return _s(v)

    result = _FORMAT_RE.sub(sub, str(fmt))
    return Unknown("format with unknown argument") if unknown else result


def _format_number(v: float, spec: str) -> str:
    kind, digits = spec[:1].upper(), spec[1:]
    n = int(digits) if digits.isdigit() else 0
    if kind == "D":
        return str(int(v)).zfill(n)
    if kind == "N":
        return f"{v:,.{n or 2}f}"
    if kind == "F":
        return f"{v:.{n or 2}f}"
    if kind == "X":
        return f"{int(v):X}".zfill(n)
    return _s(v)


def _fn_create_object(args: list[JsonValue], _: EvalContext) -> JsonValue:
    if len(args) % 2:
        raise ArmError("createObject needs key/value pairs")
    out: dict[str, JsonValue] = {}
    for i in range(0, len(args), 2):
        key = args[i]
        if isinstance(key, Unknown):
            return key
        out[_s(key)] = args[i + 1]
    return out


def _fn_array(args: list[JsonValue], _: EvalContext) -> JsonValue:
    v = args[0]
    if isinstance(v, list):
        return v
    return [v]


def _fn_union(args: list[JsonValue], _: EvalContext) -> JsonValue:
    if any(isinstance(a, Unknown) for a in args):
        return Unknown("union with unknown")
    dicts = [a for a in args if isinstance(a, dict)]
    if len(dicts) == len(args):
        merged: dict[str, JsonValue] = {}
        for d in dicts:
            merged.update(d)
        return merged
    lists = [a for a in args if isinstance(a, list)]
    if len(lists) == len(args):
        out: list[JsonValue] = []
        for lst in lists:
            for item in lst:
                if item not in out:
                    out.append(item)
        return out
    raise ArmError("union needs all objects or all arrays")


def _fn_intersection(args: list[JsonValue], _: EvalContext) -> JsonValue:
    first = args[0]
    if isinstance(first, dict):
        return {k: v for k, v in first.items() if all(isinstance(a, dict) and k in a and a[k] == v for a in args[1:])}
    if isinstance(first, list):
        return [x for x in first if all(isinstance(a, list) and x in a for a in args[1:])]
    raise ArmError("intersection needs objects or arrays")


def _fn_length(args: list[JsonValue], _: EvalContext) -> JsonValue:
    v = args[0]
    if isinstance(v, (str, list, dict)):
        return len(v)
    raise ArmError("length needs string/array/object")


def _fn_empty(args: list[JsonValue], _: EvalContext) -> JsonValue:
    v = args[0]
    if v is None:
        return True
    if isinstance(v, (str, list, dict)):
        return len(v) == 0
    raise ArmError("empty needs string/array/object")


def _fn_contains(args: list[JsonValue], _: EvalContext) -> JsonValue:
    container, item = args
    if isinstance(container, dict):
        return isinstance(item, str) and any(k.lower() == item.lower() for k in container)
    if isinstance(container, list):
        return item in container
    if isinstance(container, str):
        return _s(item).lower() in container.lower()
    raise ArmError("contains: bad container")


def _fn_equals(args: list[JsonValue], _: EvalContext) -> JsonValue:
    a, b = args
    if isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    return a == b


def _fn_replace(args: list[JsonValue], _: EvalContext) -> JsonValue:
    return _s(args[0]).replace(_s(args[1]), _s(args[2]))


def _fn_split(args: list[JsonValue], _: EvalContext) -> JsonValue:
    delim = args[1]
    text = _s(args[0])
    delims = [delim] if isinstance(delim, str) else [_s(d) for d in delim] if isinstance(delim, list) else []
    parts = [text]
    for d in delims:
        parts = [p for part in parts for p in part.split(d)]
    return list(parts)


def _fn_substring(args: list[JsonValue], _: EvalContext) -> JsonValue:
    text = _s(args[0])
    start = int(_num(args[1]))
    if len(args) > 2:
        return text[start : start + int(_num(args[2]))]
    return text[start:]


def _fn_first(args: list[JsonValue], _: EvalContext) -> JsonValue:
    v = args[0]
    if isinstance(v, Unknown):
        return v
    if isinstance(v, str):
        return v[:1]
    if isinstance(v, list):
        return v[0] if v else None
    raise ArmError("first: bad argument")


def _fn_last(args: list[JsonValue], _: EvalContext) -> JsonValue:
    v = args[0]
    if isinstance(v, Unknown):
        return v
    if isinstance(v, str):
        return v[-1:]
    if isinstance(v, list):
        return v[-1] if v else None
    raise ArmError("last: bad argument")


def _fn_take(args: list[JsonValue], _: EvalContext) -> JsonValue:
    v, n = args[0], args[1]
    if isinstance(v, Unknown):
        return v
    if isinstance(v, (str, list)) and isinstance(n, int):
        return v[: max(n, 0)]
    raise ArmError("take: bad arguments")


def _fn_skip(args: list[JsonValue], _: EvalContext) -> JsonValue:
    v, n = args[0], args[1]
    if isinstance(v, Unknown):
        return v
    if isinstance(v, (str, list)) and isinstance(n, int):
        return v[max(n, 0) :]
    raise ArmError("skip: bad arguments")


def _fn_index_of(args: list[JsonValue], _: EvalContext) -> JsonValue:
    return _s(args[0]).lower().find(_s(args[1]).lower())


def _fn_last_index_of(args: list[JsonValue], _: EvalContext) -> JsonValue:
    return _s(args[0]).lower().rfind(_s(args[1]).lower())


def _fn_int(args: list[JsonValue], _: EvalContext) -> JsonValue:
    v = args[0]
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    return int(_s(v).strip())


def _fn_bool(args: list[JsonValue], _: EvalContext) -> JsonValue:
    v = args[0]
    if isinstance(v, str):
        lowered = v.strip().lower()
        if lowered in ("true", "1"):
            return True
        if lowered in ("false", "0"):
            return False
        raise ArmError(f"cannot convert {v!r} to bool")
    return bool(v)


def _fn_json(args: list[JsonValue], _: EvalContext) -> JsonValue:
    return json.loads(_s(args[0]))


def _fn_guid(args: list[JsonValue], _: EvalContext) -> JsonValue:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, "-".join(_s(a) for a in args)))


_B32 = "abcdefghijklmnopqrstuvwxyz234567"


def _fn_unique_string(args: list[JsonValue], _: EvalContext) -> JsonValue:
    # Not Azure's exact MurmurHash64: only stability and shape (13 base-32 chars) matter for cost estimates.
    digest = int.from_bytes(hashlib.sha256("-".join(_s(a) for a in args).encode()).digest()[:8], "big")
    return "".join(_B32[(digest >> (5 * i)) & 31] for i in range(13))


def _num(v: JsonValue) -> int | float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ArmError(f"expected number, got {v!r}")
    return v


def _fn_div(args: list[JsonValue], _: EvalContext) -> JsonValue:
    a, b = _num(args[0]), _num(args[1])
    if isinstance(a, int) and isinstance(b, int):
        return int(a / b)
    return a / b


def _fn_mod(args: list[JsonValue], _: EvalContext) -> JsonValue:
    return int(_num(args[0])) % int(_num(args[1]))


def _fn_range(args: list[JsonValue], _: EvalContext) -> JsonValue:
    start, count = int(_num(args[0])), int(_num(args[1]))
    return list(range(start, start + count))


def _fn_try_get(args: list[JsonValue], _: EvalContext) -> JsonValue:
    target = args[0]
    for key in args[1:]:
        if isinstance(target, Unknown):
            return target
        value = get_member(target, key)
        if isinstance(value, Unknown):
            if isinstance(target, dict):
                return None
            return value
        target = value
    return target


def _fn_shallow_merge(args: list[JsonValue], _: EvalContext) -> JsonValue:
    arr = args[0]
    if not isinstance(arr, list):
        raise ArmError("shallowMerge needs an array")
    merged: dict[str, JsonValue] = {}
    for item in arr:
        if isinstance(item, dict):
            merged.update(item)
    return merged


def _fn_object_keys(args: list[JsonValue], _: EvalContext) -> JsonValue:
    v = args[0]
    if not isinstance(v, dict):
        raise ArmError("objectKeys needs an object")
    return list(v.keys())


def _fn_pad_left(args: list[JsonValue], _: EvalContext) -> JsonValue:
    pad = _s(args[2])[:1] if len(args) > 2 else " "
    return _s(args[0]).rjust(int(_num(args[1])), pad)


def _fn_uri(args: list[JsonValue], _: EvalContext) -> JsonValue:
    return urllib.parse.urljoin(_s(args[0]), _s(args[1]))


def _fn_string_compare(op: Callable[[float, float], bool]) -> _Fn:
    def fn(args: list[JsonValue], _: EvalContext) -> JsonValue:
        a, b = args
        if isinstance(a, str) and isinstance(b, str):
            return op(0 if a.lower() == b.lower() else (1 if a.lower() > b.lower() else -1), 0)
        return op(_num(a), _num(b))

    return fn


def _fn_min(args: list[JsonValue], _: EvalContext) -> JsonValue:
    nums = args[0] if len(args) == 1 and isinstance(args[0], list) else args
    return min(_num(n) for n in nums)  # type: ignore[union-attr]


def _fn_max(args: list[JsonValue], _: EvalContext) -> JsonValue:
    nums = args[0] if len(args) == 1 and isinstance(args[0], list) else args
    return max(_num(n) for n in nums)  # type: ignore[union-attr]


_ID_TYPE = re.compile(r"^[A-Za-z0-9_.]+/[A-Za-z0-9_./-]+$")


def build_resource_id(prefix: str, type_: str, names: Sequence[str]) -> str:
    namespace, _, rest = type_.partition("/")
    type_parts = rest.split("/")
    segments: list[str] = [namespace]
    for t, n in zip(type_parts, names, strict=False):
        segments += [t, n]
    return f"{prefix}/providers/{'/'.join(segments)}"


def _split_resource_args(args: Sequence[JsonValue]) -> tuple[list[str], str, list[str]]:
    strs = [_s(a) for a in args]
    for p in range(min(3, len(strs))):
        t = strs[p]
        if _ID_TYPE.match(t) and len(strs) - p - 1 == t.count("/"):
            return strs[:p], t, strs[p + 1 :]
    # Names with extra segments (e.g. "parent/child" given as one name) — best effort.
    for p in range(min(3, len(strs))):
        if _ID_TYPE.match(strs[p]):
            names = strs[p + 1 :]
            flat: list[str] = []
            for n in names:
                flat += n.split("/")
            return strs[:p], strs[p], flat
    raise ArmError("cannot interpret resourceId arguments")


def _fn_resource_id(args: list[JsonValue], ctx: EvalContext) -> JsonValue:
    prefix_args, type_, names = _split_resource_args(args)
    sub, rg = ctx.default_subscription_id, ctx.default_resource_group
    if len(prefix_args) == 1:
        rg = prefix_args[0]
    elif len(prefix_args) == 2:
        sub, rg = prefix_args
    return build_resource_id(f"/subscriptions/{sub}/resourceGroups/{rg}", type_, names)


def _fn_subscription_resource_id(args: list[JsonValue], ctx: EvalContext) -> JsonValue:
    prefix_args, type_, names = _split_resource_args(args)
    sub = prefix_args[0] if prefix_args else ctx.default_subscription_id
    return build_resource_id(f"/subscriptions/{sub}", type_, names)


def _fn_extension_resource_id(args: list[JsonValue], _ctx: EvalContext) -> JsonValue:
    base = _s(args[0])
    _prefix, type_, names = _split_resource_args(args[1:])
    return build_resource_id(base, type_, names)


def _fn_tenant_resource_id(args: list[JsonValue], _ctx: EvalContext) -> JsonValue:
    _prefix, type_, names = _split_resource_args(args)
    return build_resource_id("", type_, names)


def _fn_environment(_: list[JsonValue], __: EvalContext) -> JsonValue:
    return {
        "name": "AzureCloud",
        "gallery": "https://gallery.azure.com/",
        "graph": "https://graph.windows.net/",
        "portal": "https://portal.azure.com",
        "graphAudience": "https://graph.windows.net/",
        "activeDirectoryDataLake": "https://datalake.azure.net/",
        "batch": "https://batch.core.windows.net/",
        "media": "https://rest.media.azure.net",
        "sqlManagement": "https://management.core.windows.net:8443/",
        "vmImageAliasDoc": "https://raw.githubusercontent.com/Azure/azure-rest-api-specs/master/arm-compute/quickstart-templates/aliases.json",
        "resourceManager": "https://management.azure.com/",
        "authentication": {
            "loginEndpoint": "https://login.microsoftonline.com/",
            "audiences": ["https://management.core.windows.net/", "https://management.azure.com/"],
            "tenant": "common",
            "identityProvider": "AAD",
        },
        "suffixes": {
            "acrLoginServer": ".azurecr.io",
            "azureDatalakeAnalyticsCatalogAndJob": "azuredatalakeanalytics.net",
            "azureDatalakeStoreFileSystem": "azuredatalakestore.net",
            "azureFrontDoorEndpointSuffix": "azurefd.net",
            "keyvaultDns": ".vault.azure.net",
            "sqlServerHostname": ".database.windows.net",
            "storage": "core.windows.net",
        },
    }


def _fn_tenant(_: list[JsonValue], __: EvalContext) -> JsonValue:
    return {
        "tenantId": "00000000-0000-0000-0000-000000000000",
        "displayName": "estimate",
    }


def _fn_copy_index(args: list[JsonValue], ctx: EvalContext) -> JsonValue:
    loop = None
    offset = 0
    if args and isinstance(args[0], str):
        loop = args[0]
        if len(args) > 1:
            offset = int(_num(args[1]))
    elif args:
        offset = int(_num(args[0]))
    return ctx.copy_index(loop) + offset


_FUNCTIONS: dict[str, _Fn] = {
    "parameters": lambda a, c: c.get_parameter(_s(a[0])),
    "variables": lambda a, c: c.get_variable(_s(a[0])),
    "concat": _fn_concat,
    "format": _fn_format,
    "createobject": _fn_create_object,
    "createarray": lambda a, c: list(a),
    "array": _fn_array,
    "union": _fn_union,
    "intersection": _fn_intersection,
    "length": _fn_length,
    "empty": _fn_empty,
    "contains": _fn_contains,
    "equals": _fn_equals,
    "not": lambda a, c: not _truthy(a[0]),
    "replace": _fn_replace,
    "split": _fn_split,
    "substring": _fn_substring,
    "tolower": lambda a, c: _s(a[0]).lower(),
    "toupper": lambda a, c: _s(a[0]).upper(),
    "trim": lambda a, c: _s(a[0]).strip(),
    "startswith": lambda a, c: _s(a[0]).lower().startswith(_s(a[1]).lower()),
    "endswith": lambda a, c: _s(a[0]).lower().endswith(_s(a[1]).lower()),
    "indexof": _fn_index_of,
    "lastindexof": _fn_last_index_of,
    "padleft": _fn_pad_left,
    "first": _fn_first,
    "last": _fn_last,
    "take": _fn_take,
    "skip": _fn_skip,
    "string": lambda a, c: _s(a[0]),
    "int": _fn_int,
    "bool": _fn_bool,
    "json": _fn_json,
    "base64": lambda a, c: base64.b64encode(_s(a[0]).encode()).decode(),
    "base64tostring": lambda a, c: base64.b64decode(_s(a[0])).decode(),
    "uri": _fn_uri,
    "uricomponent": lambda a, c: urllib.parse.quote(_s(a[0]), safe="-_.~"),
    "guid": _fn_guid,
    "uniquestring": _fn_unique_string,
    "add": lambda a, c: _num(a[0]) + _num(a[1]),
    "sub": lambda a, c: _num(a[0]) - _num(a[1]),
    "mul": lambda a, c: _num(a[0]) * _num(a[1]),
    "div": _fn_div,
    "mod": _fn_mod,
    "min": _fn_min,
    "max": _fn_max,
    "range": _fn_range,
    "less": _fn_string_compare(lambda a, b: a < b),
    "lessorequals": _fn_string_compare(lambda a, b: a <= b),
    "greater": _fn_string_compare(lambda a, b: a > b),
    "greaterorequals": _fn_string_compare(lambda a, b: a >= b),
    "tryget": _fn_try_get,
    "shallowmerge": _fn_shallow_merge,
    "objectkeys": _fn_object_keys,
    "true": lambda a, c: True,
    "false": lambda a, c: False,
    "null": lambda a, c: None,
    "copyindex": _fn_copy_index,
    "resourceid": _fn_resource_id,
    "subscriptionresourceid": _fn_subscription_resource_id,
    "extensionresourceid": _fn_extension_resource_id,
    "tenantresourceid": _fn_tenant_resource_id,
    "reference": lambda a, c: c.reference(a),
    "resourcegroup": lambda a, c: c.resource_group(),
    "subscription": lambda a, c: c.subscription(),
    "deployment": lambda a, c: c.deployment(),
    "environment": _fn_environment,
    "tenant": _fn_tenant,
}
