"""Result types of an estimate."""

from dataclasses import dataclass, field
from typing import Literal

from .engine import Resource
from .prices import Meter

# fixed: follows from the template; usage: depends on assumed consumption; free: costs nothing.
CostBasis = Literal["fixed", "usage"]
Status = Literal["priced", "free", "included", "external", "unsupported", "unresolved"]


@dataclass
class LineItem:
    description: str
    quantity: float
    basis: CostBasis
    meter: Meter | None = None
    # Items with the same meter and scope share one tier ladder / free grant (e.g. per subscription or workspace).
    scope: str | None = None
    # Free units applied once per (meter, scope) group, for grants the price API does not model.
    grant: float = 0.0
    # Cost of an item without a meter (user-supplied amount).
    fixed_amount: float | None = None
    note: str | None = None
    monthly: float = 0.0

    @property
    def unit(self) -> str:
        return self.meter.unit if self.meter else ""


@dataclass
class ResourceEstimate:
    resource: Resource
    status: Status
    items: list[LineItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def monthly(self) -> float:
        return sum(i.monthly for i in self.items)

    def subtotal(self, basis: CostBasis) -> float:
        return sum(i.monthly for i in self.items if i.basis == basis)


@dataclass
class Estimate:
    currency: str
    region_default: str
    resources: list[ResourceEstimate]
    warnings: list[str]
    assumptions: list[str]
    default_assumptions: int = 0

    @property
    def monthly(self) -> float:
        return sum(r.monthly for r in self.resources)

    @property
    def fixed(self) -> float:
        return sum(r.subtotal("fixed") for r in self.resources)

    @property
    def usage(self) -> float:
        return sum(r.subtotal("usage") for r in self.resources)

    def count(self, status: Status) -> int:
        return sum(1 for r in self.resources if r.status == status)
