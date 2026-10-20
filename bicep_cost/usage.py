"""Usage assumptions for consumption-priced meters (storage GB, log volume, session hours, ...).

Layout of the optional TOML file::

    [storage]                      # applies to every storage account
    stored_gb = 200

    [storage."stprodapp*"]   # glob on the resource name, most specific wins
    stored_gb = 2000

    [containerapps."app-api"]
    active_percent = 60
"""

import fnmatch
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

Scalar = float | int | str | bool


class UsageError(ValueError):
    """The usage file contains a value that cannot be used."""


def _merge(into: dict[str, object], other: dict[str, object]) -> None:
    for k, v in other.items():
        current = into.get(k)
        if isinstance(v, dict) and isinstance(current, dict):
            _merge(current, v)
        else:
            into[k] = v


@dataclass(frozen=True)
class Assumption:
    section: str
    name: str
    key: str
    value: Scalar
    from_default: bool

    def render(self) -> str:
        origin = "default" if self.from_default else "usage file"
        return f"{self.section}[{self.name}].{self.key} = {self.value} ({origin})"


@dataclass
class UsageConfig:
    data: dict[str, object] = field(default_factory=dict)
    used: dict[tuple[str, str, str], Assumption] = field(default_factory=dict)

    @classmethod
    def load(cls, paths: Sequence[Path] = ()) -> "UsageConfig":
        """Load and merge usage files; later files override earlier ones key by key."""
        data: dict[str, object] = {}
        for path in paths:
            with path.open("rb") as fh:
                try:
                    _merge(data, tomllib.load(fh))
                except tomllib.TOMLDecodeError as exc:
                    raise UsageError(f"{path}: {exc}") from exc
        return cls(data)

    @property
    def cost_currency(self) -> str | None:
        meta = self.data.get("meta")
        value = meta.get("cost_currency") if isinstance(meta, dict) else None
        return value if isinstance(value, str) else None

    def discount_percent(self, resource_type: str) -> float:
        """Percent off list price for a resource type (``[discounts]`` table; ``default`` applies to the rest)."""
        table = self.data.get("discounts")
        if not isinstance(table, dict):
            return 0.0
        lowered = {k.lower(): v for k, v in table.items()}
        value = lowered.get(resource_type.lower(), lowered.get("default", 0.0))
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
            raise UsageError(f"[discounts] value for {resource_type!r} must be a percentage between 0 and 100")
        return float(value)

    def has(self, section: str, name: str, key: str) -> bool:
        return self._lookup(section, name, key) is not None

    def optional_number(self, section: str, name: str, key: str) -> float | None:
        """A number set in the usage file, or None (without recording a default assumption)."""
        if not self.has(section, name, key):
            return None
        return self.number(section, name, key, 0.0)

    def _lookup(self, section: str, name: str, key: str) -> Scalar | None:
        table = self.data.get(section)
        if not isinstance(table, dict):
            return None
        exact: Scalar | None = None
        best_glob: tuple[tuple[int, int], Scalar] | None = None
        for k, v in table.items():
            if isinstance(v, dict) and key in v:
                if k.lower() == name.lower():
                    exact = v[key]
                elif fnmatch.fnmatchcase(name.lower(), k.lower()):
                    # most specific glob wins: more literal characters, then longer pattern
                    rank = (sum(1 for ch in k if ch not in "*?[]"), len(k))
                    if best_glob is None or rank > best_glob[0]:
                        best_glob = (rank, v[key])
        if exact is not None:
            return exact
        if best_glob is not None:
            return best_glob[1]
        return table.get(key) if not isinstance(table.get(key), dict) else None

    def get(self, section: str, name: str, key: str, default: Scalar) -> Scalar:
        found = self._lookup(section, name, key)
        value = default if found is None else found
        self.used[(section, name, key)] = Assumption(section, name, key, value, found is None)
        return value

    def number(self, section: str, name: str, key: str, default: float) -> float:
        value = self.get(section, name, key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise UsageError(f"usage {section}.{key} must be a number, got {value!r}")
        return float(value)

    def assumptions(self) -> list[Assumption]:
        """Assumptions that influenced the estimate: explicit usage-file values and non-zero defaults."""
        relevant = [a for a in self.used.values() if not (a.from_default and a.value == 0)]
        return sorted(relevant, key=lambda a: (a.section, a.name, a.key))
