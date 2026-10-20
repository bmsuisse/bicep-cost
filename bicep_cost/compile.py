"""Turn the user's input (``.bicep``, ``.bicepparam`` or ARM ``.json``) into a template plus parameter values."""

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .armexpr import JsonValue, Unknown


class CompileError(Exception):
    pass


@dataclass
class CompiledInput:
    template: dict[str, JsonValue]
    parameters: dict[str, JsonValue]
    source: str
    compiler_messages: list[str] = field(default_factory=list)


def find_bicep() -> list[str]:
    """Command prefix that runs the Bicep CLI: $BICEP_CLI, ``bicep`` on PATH, ``~/.azure/bin/bicep`` or ``az bicep``."""
    explicit = os.environ.get("BICEP_CLI")
    if explicit:
        return [explicit]
    on_path = shutil.which("bicep")
    if on_path:
        return [on_path]
    az_copy = Path.home() / ".azure" / "bin" / "bicep"
    if az_copy.exists():
        return [str(az_copy)]
    az = shutil.which("az")
    if az:
        return [az, "bicep"]
    raise CompileError(
        "Bicep CLI not found. Install it (https://aka.ms/bicep-install), set BICEP_CLI, or pass a compiled ARM .json"
    )


def _run(cmd: list[str]) -> tuple[str, list[str]]:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    messages = [ln for ln in proc.stderr.splitlines() if ln.strip()]
    if proc.returncode != 0:
        errors = [m for m in messages if "Error" in m or "ERROR" in m] or messages
        raise CompileError("bicep failed:\n" + "\n".join(errors[:20]))
    return proc.stdout, messages


def _az_style(prefix: list[str], verb: str, path: Path, extra: list[str]) -> list[str]:
    # `az bicep build --file X` vs `bicep build X`
    if prefix[-1] == "bicep" and Path(prefix[0]).name.startswith("az"):
        return [*prefix, verb, "--file", str(path), *extra, "--stdout"]
    return [*prefix, verb, str(path), *extra, "--stdout"]


def compile_bicep(path: Path) -> tuple[dict[str, JsonValue], list[str]]:
    out, msgs = _run(_az_style(find_bicep(), "build", path, []))
    return json.loads(out), msgs


def compile_bicepparam(
    path: Path, bicep_file: Path | None
) -> tuple[dict[str, JsonValue], dict[str, JsonValue] | None, list[str]]:
    extra = ["--bicep-file", str(bicep_file)] if bicep_file else []
    out, msgs = _run(_az_style(find_bicep(), "build-params", path, extra))
    payload = json.loads(out)
    params = parameters_from_arm(json.loads(payload["parametersJson"]))
    template = json.loads(payload["templateJson"]) if payload.get("templateJson") else None
    return params, template, msgs


_AZD_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:=([^}]*))?\}")


def _substitute_env(value: JsonValue) -> JsonValue:
    """azd-style ``${VAR}`` / ``${VAR=default}`` placeholders in parameter files."""
    if isinstance(value, str):
        if "${" not in value:
            return value
        unresolved: list[str] = []

        def sub(m: re.Match[str]) -> str:
            env = os.environ.get(m.group(1))
            if env is not None:
                return env
            if m.group(2) is not None:
                return m.group(2)
            unresolved.append(m.group(1))
            return ""

        result = _AZD_VAR.sub(sub, value)
        return Unknown(f"environment variable {unresolved[0]} is not set") if unresolved else result
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    return value


def parameters_from_arm(doc: dict[str, JsonValue]) -> dict[str, JsonValue]:
    raw = doc.get("parameters", doc)
    if not isinstance(raw, dict):
        raise CompileError("parameter file has no 'parameters' object")
    out: dict[str, JsonValue] = {}
    for name, entry in raw.items():
        if isinstance(entry, dict) and "value" in entry:
            out[name] = _substitute_env(entry["value"])
        elif isinstance(entry, dict) and "reference" in entry:
            out[name] = Unknown("Key Vault reference")
        elif not isinstance(entry, dict) or not ({"value", "reference"} & entry.keys()):
            out[name] = _substitute_env(entry)
    return out


def parse_set(pairs: list[str]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for pair in pairs:
        if "=" not in pair:
            raise CompileError(f"--set expects name=value, got {pair!r}")
        name, raw = pair.split("=", 1)
        try:
            result[name] = json.loads(raw)
        except json.JSONDecodeError:
            result[name] = raw
    return result


def load_input(template: Path, parameters: Path | None, overrides: list[str]) -> CompiledInput:
    messages: list[str] = []
    params: dict[str, JsonValue] = {}
    suffix = template.suffix.lower()
    tmpl: dict[str, JsonValue] | None = None
    if suffix == ".bicepparam":
        if parameters is not None:
            raise CompileError("pass either a .bicepparam as the input or --parameters, not both")
        params, tmpl, messages = compile_bicepparam(template, None)
        if tmpl is None:
            raise CompileError(f"{template} does not reference a template (`using ...`)")
    else:
        if suffix == ".bicep":
            tmpl, messages = compile_bicep(template)
        elif suffix == ".json":
            loaded = json.loads(template.read_text())
            if not isinstance(loaded, dict) or "resources" not in loaded:
                raise CompileError(f"{template} is not an ARM template")
            tmpl = loaded
        else:
            raise CompileError(f"unsupported input {template} (expected .bicep, .bicepparam or .json)")
        if parameters is not None:
            if parameters.suffix.lower() == ".bicepparam":
                params, _, more = compile_bicepparam(parameters, template if suffix == ".bicep" else None)
                messages += more
            else:
                params = parameters_from_arm(json.loads(parameters.read_text()))
    params.update(parse_set(overrides))
    return CompiledInput(template=tmpl, parameters=params, source=str(template), compiler_messages=messages)
