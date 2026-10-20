"""Key Vault (operations) and Cognitive Services / AI Foundry accounts (usage only)."""

from bicep_cost.context import EstimateContext, need_str
from bicep_cost.engine import Resource
from bicep_cost.estimators.common import line, priced, usage_amount
from bicep_cost.model import ResourceEstimate


def estimate_vault(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    region = ctx.region(res)
    sku = need_str(res, "properties.sku.name", "Key Vault SKU")
    ops_10k = ctx.usage.number("keyvault", res.name, "operations_10k", 10.0)
    meter = ctx.meter("Key Vault", region, product="Key Vault", sku=sku.capitalize(), meter="Operations")
    return priced(
        res,
        [
            line(
                f"{sku} vault operations ({ops_10k * 10_000:,.0f})",
                meter,
                ops_10k,
                "usage",
                note="assumed transactions",
            )
        ],
        ["no base fee; certificates, HSM keys and key rotation are not priced"],
    )


def estimate_cognitive_account(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    items = usage_amount(res, ctx, "cognitiveservices")
    kind = res.kind or "unknown kind"
    sku = str(res.get("sku.name") or "?")
    notes = [f"{kind} ({sku}): billed per call/token only, no base fee"]
    if not items:
        notes.append(f'token/call cost not estimated; set [cognitiveservices."{res.name}"] monthly_cost')
    return priced(res, items, notes)
