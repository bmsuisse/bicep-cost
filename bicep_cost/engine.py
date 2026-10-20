"""Expand a compiled ARM template into the flat list of resources it would deploy.

Handles parameters/variables, ``condition`` and ``copy`` (resources, properties, variables, outputs)
and inline nested deployments (Bicep modules). Anything that can only be known after deployment
becomes :class:`~bicep_cost.armexpr.Unknown`.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .armexpr import (
    JsonValue,
    Unknown,
    evaluate_string,
    get_member,
)

DEPLOYMENTS = "microsoft.resources/deployments"
MAX_COPY_COUNT = 10_000


@dataclass
class Resource:
    type: str
    name: str
    id: str
    body: dict[str, JsonValue]
    module_path: tuple[str, ...]
    subscription_id: str
    resource_group: str

    @property
    def location(self) -> str | None:
        v = self.body.get("location")
        return v if isinstance(v, str) else None

    @property
    def kind(self) -> str | None:
        v = self.body.get("kind")
        return v if isinstance(v, str) else None

    @property
    def properties(self) -> dict[str, JsonValue]:
        v = self.body.get("properties")
        return v if isinstance(v, dict) else {}

    @property
    def sku(self) -> dict[str, JsonValue]:
        v = self.body.get("sku")
        return v if isinstance(v, dict) else {}

    @property
    def address(self) -> str:
        return "/".join((*self.module_path, f"{self.type}/{self.name}"))

    def _walk(self, path: str) -> JsonValue:
        cur: JsonValue = self.body
        for part in path.split("."):
            key: JsonValue = int(part) if part.isdigit() and isinstance(cur, list) else part
            cur = get_member(cur, key)
            if isinstance(cur, Unknown):
                return cur
        return cur

    def get(self, path: str, default: JsonValue = None) -> JsonValue:
        """Read a dotted path (list indexes as numbers); missing or unknown values give ``default``."""
        value = self._walk(path)
        return default if isinstance(value, Unknown) else value

    def is_unknown_at(self, path: str) -> bool:
        """True if the path ends in (or runs through) a value that is only known after deployment.

        A property that is simply absent from the template is *not* unknown.
        """
        cur: JsonValue = self.body
        for part in path.split("."):
            if isinstance(cur, Unknown):
                return True
            if isinstance(cur, dict) and part.lower() not in {k.lower() for k in cur}:
                return False
            key: JsonValue = int(part) if part.isdigit() and isinstance(cur, list) else part
            cur = get_member(cur, key)
        return isinstance(cur, Unknown)


@dataclass(frozen=True)
class Diagnostic:
    level: str  # "warning" | "info"
    message: str
    where: str = ""


@dataclass
class DeploymentSettings:
    subscription_id: str = "00000000-0000-0000-0000-000000000000"
    resource_group: str = "rg-estimate"
    location: str = "westeurope"
    deployment_name: str = "estimate"


@dataclass
class _Instance:
    raw: dict[str, JsonValue]
    type: str
    name: str
    copy_frames: tuple[tuple[str, int], ...]
    evaluated: dict[str, JsonValue] | None = None
    outputs: dict[str, JsonValue] | None = None
    in_progress: bool = False
    resource: Resource | None = None


_GUID = re.compile(r"^[0-9a-fA-F-]{36}$")


@dataclass
class Engine:
    settings: DeploymentSettings
    resources: list[Resource] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    _placeholders: int = 0

    def placeholder_name(self) -> str:
        self._placeholders += 1
        return f"unknown-name-{self._placeholders}"

    def warn(self, message: str, where: tuple[str, ...] = ()) -> None:
        self.diagnostics.append(Diagnostic("warning", message, "/".join(where)))

    def expand(self, template: Mapping[str, JsonValue], parameters: Mapping[str, JsonValue]) -> None:
        scope = TemplateScope(
            engine=self,
            template=template,
            provided=dict(parameters),
            module_path=(),
            subscription_id=self.settings.subscription_id,
            resource_group=self.settings.resource_group,
            deployment_name=self.settings.deployment_name,
            parent=None,
        )
        scope.run()


class TemplateScope:
    def __init__(
        self,
        engine: Engine,
        template: Mapping[str, JsonValue],
        provided: dict[str, JsonValue] | None,
        module_path: tuple[str, ...],
        subscription_id: str,
        resource_group: str,
        deployment_name: str,
        parent: "TemplateScope | None",
        share_with_parent: bool = False,
    ) -> None:
        self.engine = engine
        self.template = template
        self.provided = provided or {}
        self.module_path = module_path
        self._sub = subscription_id
        self._rg = resource_group
        self.deployment_name = deployment_name
        self.parent = parent
        self.share = share_with_parent and parent is not None
        self._params_cache: dict[str, JsonValue] = {}
        self._params_in_progress: set[str] = set()
        self._vars_cache: dict[str, JsonValue] = {}
        self._vars_in_progress: set[str] = set()
        self._frames: list[tuple[str, int]] = []
        self.instances: list[_Instance] = []
        self._by_id: dict[str, _Instance] = {}
        self._deployments_by_name: dict[str, _Instance] = {}

        raw_vars = template.get("variables")
        self._raw_vars: dict[str, JsonValue] = {}
        self._var_loops: dict[str, dict[str, JsonValue]] = {}
        if isinstance(raw_vars, dict):
            for k, v in raw_vars.items():
                if k == "copy" and isinstance(v, list):
                    for loop in v:
                        if isinstance(loop, dict) and isinstance(loop.get("name"), str):
                            self._var_loops[str(loop["name"]).lower()] = loop
                else:
                    self._raw_vars[k.lower()] = v
        raw_params = template.get("parameters")
        self._param_defs: dict[str, dict[str, JsonValue]] = {}
        if isinstance(raw_params, dict):
            for k, v in raw_params.items():
                self._param_defs[k.lower()] = v if isinstance(v, dict) else {}
        self._provided_ci = {k.lower(): v for k, v in self.provided.items()}

    # -- EvalContext -------------------------------------------------------

    @property
    def default_subscription_id(self) -> str:
        return self._sub

    @property
    def default_resource_group(self) -> str:
        return self._rg

    def get_parameter(self, name: str) -> JsonValue:
        if self.share and self.parent is not None:
            return self.parent.get_parameter(name)
        key = name.lower()
        if key in self._params_cache:
            return self._params_cache[key]
        if key in self._provided_ci:
            value = self._provided_ci[key]
        elif key in self._param_defs and "defaultValue" in self._param_defs[key]:
            if key in self._params_in_progress:
                return Unknown(f"parameter {name} default is circular")
            self._params_in_progress.add(key)
            try:
                value = self.eval_value(self._param_defs[key]["defaultValue"])
            finally:
                self._params_in_progress.discard(key)
        elif key in self._param_defs:
            value = Unknown(f"parameter {name} has no value or default")
        else:
            value = Unknown(f"unknown parameter {name}")
        self._params_cache[key] = value
        return value

    def get_variable(self, name: str) -> JsonValue:
        if self.share and self.parent is not None:
            return self.parent.get_variable(name)
        key = name.lower()
        if key in self._vars_cache:
            return self._vars_cache[key]
        if key in self._vars_in_progress:
            return Unknown(f"variable {name} is circular")
        self._vars_in_progress.add(key)
        try:
            if key in self._raw_vars:
                value = self.eval_value(self._raw_vars[key])
            elif key in self._var_loops:
                value = self._eval_loop(self._var_loops[key])
            else:
                value = Unknown(f"unknown variable {name}")
        finally:
            self._vars_in_progress.discard(key)
        self._vars_cache[key] = value
        return value

    def copy_index(self, loop_name: str | None) -> int:
        if not self._frames:
            if self.share and self.parent is not None:
                return self.parent.copy_index(loop_name)
            return 0
        if loop_name is None:
            return self._frames[-1][1]
        for name, idx in reversed(self._frames):
            if name.lower() == loop_name.lower():
                return idx
        return self._frames[-1][1]

    def resource_group(self) -> JsonValue:
        return {
            "id": f"/subscriptions/{self._sub}/resourceGroups/{self._rg}",
            "name": self._rg,
            "type": "Microsoft.Resources/resourceGroups",
            "location": self.engine.settings.location,
            "tags": {},
            "properties": {"provisioningState": "Succeeded"},
        }

    def subscription(self) -> JsonValue:
        return {
            "id": f"/subscriptions/{self._sub}",
            "subscriptionId": self._sub,
            "tenantId": "00000000-0000-0000-0000-000000000000",
            "displayName": "estimate",
        }

    def deployment(self) -> JsonValue:
        return {
            "name": self.deployment_name,
            "location": self.engine.settings.location,
            "properties": {"template": {"contentVersion": "1.0.0.0"}},
        }

    def reference(self, args: Sequence[JsonValue]) -> JsonValue:
        if not args or not isinstance(args[0], str):
            return Unknown("reference() of a non-string")
        target = args[0]
        inst = self._find_instance(target)
        if inst is None:
            return Unknown(f"reference({target}) is only known after deployment")
        if inst.type.lower() == DEPLOYMENTS:
            self._ensure_evaluated(inst)
            outputs = inst.outputs if inst.outputs is not None else {}
            return {"outputs": outputs, "provisioningState": "Succeeded"}
        body = self._ensure_evaluated(inst)
        if body is None:
            return Unknown(f"reference({target}) is circular")
        full = len(args) >= 3 and isinstance(args[2], str) and args[2].lower() == "full"
        if full:
            return body
        props = body.get("properties")
        return props if isinstance(props, dict) else {}

    # -- evaluation helpers ------------------------------------------------

    def eval_value(self, value: JsonValue) -> JsonValue:
        if isinstance(value, str):
            return evaluate_string(value, self)
        if isinstance(value, list):
            return [self.eval_value(v) for v in value]
        if isinstance(value, dict):
            out: dict[str, JsonValue] = {}
            loops: list[dict[str, JsonValue]] = []
            for k, v in value.items():
                if k == "copy" and isinstance(v, list):
                    loops.extend(x for x in v if isinstance(x, dict))
                else:
                    out[k] = self.eval_value(v)
            for loop in loops:
                name = loop.get("name")
                if isinstance(name, str):
                    out[name] = self._eval_loop(loop)
            return out
        return value

    def _eval_loop(self, loop: Mapping[str, JsonValue]) -> JsonValue:
        count = self.eval_value(loop.get("count", 0))
        if not isinstance(count, int) or isinstance(count, bool):
            self.engine.warn(f"copy count of {loop.get('name')!r} is not known; assuming 0", self.module_path)
            return []
        if count > MAX_COPY_COUNT:
            self.engine.warn(
                f"copy count {count} of {loop.get('name')!r} exceeds the limit of {MAX_COPY_COUNT}; assuming 0",
                self.module_path,
            )
            return []
        name = str(loop.get("name", ""))
        items: list[JsonValue] = []
        for i in range(count):
            self._frames.append((name, i))
            try:
                items.append(self.eval_value(loop.get("input")))
            finally:
                self._frames.pop()
        return items

    # -- resource expansion ------------------------------------------------

    def run(self) -> None:
        self._collect_instances()
        for inst in list(self.instances):
            self._ensure_evaluated(inst)

    def _collect_instances(self) -> None:
        raw_resources = self.template.get("resources", [])
        entries: list[dict[str, JsonValue]] = []
        if isinstance(raw_resources, dict):
            entries = [r for r in raw_resources.values() if isinstance(r, dict) and not r.get("existing")]
        elif isinstance(raw_resources, list):
            entries = [r for r in raw_resources if isinstance(r, dict)]
        for raw in entries:
            cond = raw.get("condition")
            if cond is not None:
                cond_v = self.eval_value(cond)
                if isinstance(cond_v, Unknown):
                    self.engine.warn(
                        f"condition is not known ({cond_v.reason}); assuming the resource deploys", self.module_path
                    )
                elif not cond_v:
                    continue
            copy = raw.get("copy")
            frame_sets: list[tuple[tuple[str, int], ...]]
            if isinstance(copy, dict):
                count = self.eval_value(copy.get("count", 0))
                cname = str(copy.get("name", "copy"))
                if not isinstance(count, int) or isinstance(count, bool):
                    self.engine.warn(f"resource copy count not known for {cname!r}; assuming 0", self.module_path)
                    continue
                if count > MAX_COPY_COUNT:
                    self.engine.warn(
                        f"resource copy count {count} of {cname!r} exceeds the limit of {MAX_COPY_COUNT}; skipped",
                        self.module_path,
                    )
                    continue
                frame_sets = [((cname, i),) for i in range(count)]
            else:
                frame_sets = [()]
            for frames in frame_sets:
                self._frames.extend(frames)
                try:
                    type_ = self.eval_value(raw.get("type"))
                    name = self.eval_value(raw.get("name"))
                finally:
                    del self._frames[len(self._frames) - len(frames) :]
                if not isinstance(type_, str):
                    self.engine.warn(f"skipping resource with unresolved type ({type_!r})", self.module_path)
                    continue
                if not isinstance(name, str):
                    reason = name.reason if isinstance(name, Unknown) else repr(name)
                    name = self.engine.placeholder_name()
                    self.engine.warn(
                        f"{type_}: name not known ({reason}); using placeholder {name!r}", self.module_path
                    )
                inst = _Instance(raw=raw, type=type_, name=name, copy_frames=frames)
                self.instances.append(inst)
                self._by_id[self._resource_id(type_, name, self._sub, self._rg).lower()] = inst
                if type_.lower() == DEPLOYMENTS:
                    self._deployments_by_name[name.lower()] = inst

    @staticmethod
    def _resource_id(type_: str, name: str, sub: str, rg: str) -> str:
        from .armexpr import build_resource_id

        return build_resource_id(f"/subscriptions/{sub}/resourceGroups/{rg}", type_, name.split("/"))

    def _find_instance(self, target: str) -> _Instance | None:
        inst = self._by_id.get(target.lower())
        if inst is not None:
            return inst
        marker = "/providers/microsoft.resources/deployments/"
        low = target.lower()
        if marker in low:
            return self._deployments_by_name.get(low.rsplit("/", 1)[-1])
        inst = self._deployments_by_name.get(low)
        if inst is not None:
            return inst
        if self.share and self.parent is not None:
            return self.parent._find_instance(target)
        return None

    def _ensure_evaluated(self, inst: _Instance) -> dict[str, JsonValue] | None:
        if inst.evaluated is not None:
            return inst.evaluated
        if inst.in_progress:
            return None
        inst.in_progress = True
        self._frames.extend(inst.copy_frames)
        try:
            body: dict[str, JsonValue] = {}
            for k, v in inst.raw.items():
                if k in ("dependsOn", "copy", "condition", "comments", "metadata"):
                    continue
                if k == "properties" and inst.type.lower() == DEPLOYMENTS:
                    continue  # evaluated by _run_deployment, template must stay raw
                body[k] = self.eval_value(v)
            body["type"] = inst.type
            body["name"] = inst.name
            inst.evaluated = body
            if inst.type.lower() == DEPLOYMENTS:
                self._run_deployment(inst, body)
            else:
                self._record(inst, body)
        finally:
            for _ in inst.copy_frames:
                self._frames.pop()
            inst.in_progress = False
        return inst.evaluated

    def _record(self, inst: _Instance, body: dict[str, JsonValue]) -> None:
        rid = self._resource_id(inst.type, inst.name, self._sub, self._rg)
        body["id"] = rid
        res = Resource(
            type=inst.type,
            name=inst.name,
            id=rid,
            body=body,
            module_path=self.module_path,
            subscription_id=self._sub,
            resource_group=self._rg,
        )
        inst.resource = res
        self.engine.resources.append(res)

    def _run_deployment(self, inst: _Instance, body: dict[str, JsonValue]) -> None:
        props = inst.raw.get("properties")
        if not isinstance(props, dict):
            return
        template = props.get("template")
        if not isinstance(template, dict):
            self.engine.warn(
                f"deployment {inst.name!r} has no inline template (templateLink is unsupported)", self.module_path
            )
            return
        sub = self._sub
        rg = self._rg
        sub_v = body.get("subscriptionId")
        if isinstance(sub_v, str):
            sub = sub_v
        rg_v = body.get("resourceGroup")
        if isinstance(rg_v, str):
            rg = rg_v
        options = props.get("expressionEvaluationOptions")
        inner = isinstance(options, dict) and str(options.get("scope", "")).lower() == "inner"
        provided: dict[str, JsonValue] = {}
        raw_params = props.get("parameters")
        if isinstance(raw_params, dict):
            for pname, pdef in raw_params.items():
                # An entry is {"value": x}, {"reference": ...}, or an expression yielding one of those.
                entry = (
                    pdef
                    if isinstance(pdef, dict) and ("value" in pdef or "reference" in pdef)
                    else self.eval_value(pdef)
                )
                if isinstance(entry, dict) and "value" in entry:
                    provided[pname] = self.eval_value(entry["value"]) if entry is pdef else entry["value"]
                elif isinstance(entry, dict) and "reference" in entry:
                    provided[pname] = Unknown("Key Vault reference")
                elif isinstance(entry, Unknown):
                    provided[pname] = entry
        child = TemplateScope(
            engine=self.engine,
            template=template,
            provided=provided,
            module_path=(*self.module_path, inst.name),
            subscription_id=sub,
            resource_group=rg,
            deployment_name=inst.name,
            parent=self,
            share_with_parent=not inner,
        )
        child.run()
        inst.outputs = child.evaluate_outputs()

    def evaluate_outputs(self) -> dict[str, JsonValue]:
        outputs = self.template.get("outputs")
        result: dict[str, JsonValue] = {}
        if not isinstance(outputs, dict):
            return result
        for name, out in outputs.items():
            if not isinstance(out, dict):
                continue
            cond = out.get("condition")
            if cond is not None:
                cv = self.eval_value(cond)
                if not isinstance(cv, Unknown) and not cv:
                    continue
            copy = out.get("copy")
            if isinstance(copy, dict):
                value = self._eval_loop({"name": name, "count": copy.get("count"), "input": copy.get("input")})
            else:
                value = self.eval_value(out.get("value"))
            result[name] = {"type": out.get("type"), "value": value}
        return result
