# bicep-cost

Estimate the monthly Azure cost of a Bicep (or compiled ARM) deployment from the command line, using the public
[Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices)
(the same list prices the Azure pricing calculator uses). No Azure login needed.

```bash
uv tool install .            # or: uvx --from . bicep-cost ...

bicep-cost estimate app/infra/main.bicep --set administratorLoginPassword=x
bicep-cost estimate other-app/infra/parameters/prod.bicepparam          # uses the file's `using` template
bicep-cost estimate main.bicep -p parameters/prod.bicepparam --currency CHF --detail
bicep-cost estimate compiled.json -p main.parameters.json --format markdown   # azd ${VAR} placeholders read from env
```

```
RESOURCE                                      FIXED        USAGE*         TOTAL
serverfarms: app-plan                     259.88          0.00        259.88
containerApps: app-api                    217.28          0.00        217.28
flexibleServers: app-postgres             152.57          0.00        152.57
...
Provisioned (fixed):                869.11 USD
Usage-based* (assumed):              43.91 USD
TOTAL per month:                    913.02 USD
```

## How it works

1. `.bicep` / `.bicepparam` are compiled with the Bicep CLI (`$BICEP_CLI`, `bicep` on PATH, `~/.azure/bin/bicep`, or
   `az bicep`). A compiled ARM `.json` skips this step.
2. A small ARM evaluator expands the template: parameters, variables, `condition`, `copy` loops, nested deployments
   (Bicep modules) and their outputs. Values that only exist after deployment (`reference()` of live resources,
   `listKeys()`, unset parameters) become *unknown* and never silently turn into guesses.
3. One estimator per resource type turns resources into meter quantities; prices are looked up in the Retail Prices
   API (cached in `~/.cache/bicep-cost`, 7 days) and tiers / free grants are applied per subscription or workspace.

## Fixed vs. usage-based

Every line is either **fixed** (follows from the template: plan SKU x instances, database compute, always-on
replicas, private endpoint hours, alert rules, cron-scheduled replicas) or **usage-based\*** (depends on traffic or
data volume: storage GB and operations, log ingestion, session hours, function executions, AI tokens). Without
measurements, usage-based numbers come from small built-in defaults (e.g. 10 GB per storage account, 2 GB of logs) -
good enough to see the shape, not to quote. `--detail` lists every assumption used and the report says how many
figures are still defaults.

### Measure the real values from a deployed environment

```bash
az login
bicep-cost collect app/infra/main.bicep --set administratorLoginPassword=x \
    --resource-group my-resource-group                 # -> main.usage.toml (next to the template)
bicep-cost collect other-app/infra/main.bicep -p other-app/infra/parameters/prod.bicepparam \
    --resource-group <rg> --days 60                            # -> parameters/prod.usage.toml

bicep-cost estimate app/infra/main.bicep ...                 # picks main.usage.toml up automatically
```

`collect` is **read-only** (GETs, Azure Monitor metric reads and two read-only POST queries: Log Analytics `Usage` and
Cost Management `query`). It evaluates the template with the real subscription and resource group so that resource ids
match the deployed ones, then writes a commented usage file:

| Resource | Measured from | Usage key |
|---|---|---|
| Storage account | blob service `BlobCapacity`, `Transactions` by `ApiName` (read / write / other) | `stored_gb`, `*_operations_10k` |
| File share | `FileCapacity`, `Transactions` (account total split over its shares on standard accounts) | `used_gb`, `*_operations_10k` |
| Log Analytics | `Usage` table (billable GB) + top tables; App Insights in the same workspace is set to 0 | `ingest_gb_per_month` |
| Container App | `Replicas` (hourly), `UsageNanoCores`/`RxBytes`/`TxBytes` (5-min idle share), `Requests` | `replica_hours`, `idle_percent`, `requests_millions` |
| Session pool, AI/Foundry account | Cost Management actual cost (pool metrics do not reflect billed session-hours) | `monthly_cost` |
| Function app (Consumption/Flex) | `FunctionExecutionCount`, `FunctionExecutionUnits` | `executions_per_month`, `gb_seconds_per_month` |
| Key Vault / private endpoint / private DNS zone / Service Bus | `ServiceApiHit` / `PEBytesIn/Out` / `QueryVolume` / message counts | `operations_10k` / `*_gb` / `queries_millions` / `operations_millions` |
| App Service plan, PostgreSQL | live resource: instance count, `storageSizeGB` (auto-grow) | `instances`, `storage_gb` |

It also prints **drift** (template says 32 GB / 2 instances, live says 64 GB / 3) and each resource's actual cost of the
window as a comment, so you can sanity-check the estimate. Files are merged key by key (`--usage a.toml --usage
b.toml`, later wins), so hand-written overrides can live in a separate file. `bicep-cost init-usage main.bicep` writes a
file with every tunable value and its default instead.

Usage file lookup for `estimate`: explicit `--usage` (repeatable), else `bicep-cost.usage.toml` and
`<params-or-template stem>.usage.toml` next to the input. Tables match the resource name, exact or glob
(`[storage."stprod*"]`); see [`examples/usage.toml`](examples/usage.toml).

Caveats of the measurements: windows are scaled to a month (730 h); `idle_percent` is judged on 5-minute windows and
can be lower than what is billed; Container Apps free grants are subscription-wide, so if other apps in the
subscription use them up, the estimate understates; `monthly_cost` values are in the billing currency recorded as
`[meta] cost_currency` (the estimate then defaults to that currency, no conversion happens).

### Negotiated discounts

List prices are what the API returns. If you pay less (EA/MCA discounts, reservations), say so per resource type:

```toml
[discounts]
"Microsoft.Web/serverfarms" = 30        # percent off list
"Microsoft.DBforPostgreSQL/flexibleServers" = 30
default = 0
```

## Supported resource types and tiers

| Area | Types | Tiers / SKUs covered |
|---|---|---|
| App Service | `serverfarms`, `sites`, `slots` | Free; Basic B1-3; Standard S1-3; Premium v2/v3/v4 (incl. `P1mv3`); Isolated v2 (stamp fee not included); Linux and Windows. Sites/slots ride on the plan; sites on plans outside the template are reported as *external* |
| Functions / Logic Apps | `serverfarms` + `sites` | Consumption (Y1), Flex (FC1: always-ready + on-demand), Elastic Premium EP1-3, Logic Apps Standard WS1-3 |
| Container Apps | `managedEnvironments`, `containerApps`, `sessionPools` | Consumption profile (active/idle rates, free grants, KEDA cron schedules), dedicated profiles D4-D32 / E4-E32 (+ plan management fee), dynamic sessions (Python/Node) and custom-container pools |
| Storage | `storageAccounts`, file `shares` | LRS / ZRS / GRS / RA-GRS / GZRS / RA-GZRS; Hot / Cool / Cold; Blob, ADLS Gen2 (HNS), standard file shares (TransactionOptimized / Hot / Cool). **Not** Premium |
| PostgreSQL | `flexibleServers` | Burstable, General Purpose, Memory Optimized (any series); HA doubles compute + storage; Premium SSD v2 IOPS / throughput; backup storage above the free allowance (geo-redundant aware) |
| Azure SQL | `servers/databases`, `servers/elasticPools`, `managedInstances` | vCore: General Purpose (provisioned and serverless), Business Critical, Hyperscale (+ HA replicas), zone redundancy, Gen5 / DC / Fsv2 / M families; DTU: Basic, Standard S0-S12, Premium P1-P15; DTU elastic pools; Managed Instance GP / BC. Databases in a pool are included. Usage keys use the full name `server/database`. Azure Hybrid Benefit is not applied (price shown includes the license). **Not** Synapse dedicated pools, LTR backups |
| Virtual machines | `virtualMachines`, `virtualMachineScaleSets`, `disks`, `publicIPAddresses` | any VM size (by `armSkuName`), Linux / Windows / Hybrid Benefit, Spot; OS + data disks (Premium / Standard SSD / Standard HDD, LRS / ZRS, tier from size); ephemeral OS disks free; VMSS = instances x VM (set `instances` for autoscale). **Not** Ultra / Premium SSD v2 disks, SQL / RHEL / SUSE license surcharges, load balancers, gateways |
| Service Bus | `namespaces` | Basic, Standard (base fee + tiered operations, first 13 M included), Premium (1/2/4/8/16 messaging units) |
| Monitoring | `workspaces`, `components`, `scheduledQueryRules` | PerGB2018 (free 5 GB, tiers), commitment tiers, retention > 31 d. **Not** Basic/Auxiliary table plans |
| Key Vault | `vaults` | Standard / Premium operations. **Not** HSM keys or managed HSM |
| Network | `privateEndpoints`, `privateDnsZones` | per-hour + data processed; zone tiers (first 25 zones). **Not** gateways, firewalls, public IPs |
| AI | `CognitiveServices/accounts` | no base fee; spend via `monthly_cost` (measured by `collect`). **Not** provisioned throughput (PTU) |
| Free (explicitly known) | role assignments, VNets/subnets, DNS records and links, budgets, dashboards, diagnostic settings, secrets, access policies, databases/configs, blob containers, queues/topics, ... | |

Anything else is listed under *Not covered by this tool (cost NOT included)*; use `--strict` to fail in that case.
Premium storage, ASE stamps, bandwidth/egress and reservations are not modelled; prices are pay-as-you-go **list
prices** unless you add `[discounts]` or opt into account price sheets (below).

Add a resource type by writing `estimate_x(res, ctx) -> ResourceEstimate` in `bicep_cost/estimators/` and registering it
in `estimators/__init__.py`.

## Other commands and flags

```bash
bicep-cost estimate ... --format json -o head.json     # also: markdown (PR comments), csv
bicep-cost diff base.json head.json                    # per-resource cost change between two reports
bicep-cost estimate ... --fail-above 1500              # exit 3 above a monthly budget (CI guard rail)
bicep-cost estimate ... --strict                       # exit 2 if something could not be priced
bicep-cost estimate ... --group-by type|module         # summary table instead of per-resource rows
bicep-cost prices --service "Azure Container Apps" --meter-contains Idle   # browse meters
```

Other options: `--location` (default region and `resourceGroup().location`, default `westeurope`), `--resource-group`,
`--subscription-id`, `--offline` / `--refresh` / `--no-cache`, `-v` (log API calls).

## Development

```bash
uv sync --group test --group dev
uv run -m pytest
uv run ruff check . && uv run ty check
```

## Currency

Estimates use **USD** by default. Set `--currency CHF` (or `EUR`, for example) on
`estimate` and `prices` to request that currency directly from Azure:

```sh
bicep-cost estimate main.bicep --currency CHF
bicep-cost prices --service "Key Vault" --currency EUR
```

For estimates the precedence is `--currency`, then `[meta] cost_currency` in the
usage TOML, then USD. Codes are case insensitive. Reports and budget limits use
the selected currency; cached prices are isolated by currency. Manual
`monthly_cost` values must use the same currency as the estimate; conflicting
usage-file metadata is rejected. No exchange-rate conversion is performed.
Diffs require both reports to use the same currency.

Azure's non-USD retail rates are budget estimates, not negotiated billing rates.
See [Microsoft's Retail Prices API currency documentation](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices).

## Percentage cost caps in CI

Save a baseline estimate (using the same currency and usage assumptions), then
fail if the new monthly cost grows by more than 10 percent:

```sh
bicep-cost estimate main.bicep --format json -o base.json
# After changing the template:
bicep-cost estimate main.bicep --baseline base.json --fail-increase-percent 10 --fail-above 1500
# Or compare two saved JSON reports:
bicep-cost diff base.json head.json --fail-increase-percent 10 --fail-above 1500
```

Either exceeded cap returns **3**. Equality and decreases pass; any positive cost
from a zero baseline fails the relative cap. Thresholds must be finite and
nonnegative. The full report is written before a budget failure, including with
`--output`, so CI can always retain it. `--strict` failures still take precedence
(exit 2). Baseline reports must use the same currency; no conversion is applied.

## Cost breakdown charts

Text and Markdown estimates include horizontal bars ordered from the most
expensive resource to the least. Each bar shows the monthly amount and share of
the known total. Text `--group-by type` or `--group-by module` aggregates both the
chart and table. Zero-cost resources do not get bars; unresolved and unsupported
resources remain listed below, since their costs are not included in the total.
Charts are plain text for readable CI logs and Markdown PR comments, and are
written even when a budget cap fails. JSON and CSV remain machine-readable.

## Automatic releases

Like [bmsui](https://github.com/bmsuisse/bmsui/blob/main/.github/workflows/release.yml),
main pushes run reusable CI, detect a new package version, publish to PyPI using
OIDC, and create a GitHub release with wheel/sdist assets. PR CI checks Ruff, ty,
tests, builds and an installed-wheel smoke test on Python 3.11 and 3.14.

Before the first merge, a PyPI maintainer must register a
[pending trusted publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/):
project **bicep-cost**, owner **bmsuisse**, repository **bicep-cost**, workflow
**release.yml**, environment **pypi**. Use the same environment in GitHub;
restrict its deployment branches to `main`. No long-lived PyPI token is needed.

Bump `[project] version` in `pyproject.toml`, run `uv lock`, and merge the PR.
Main publishes that version after CI passes; an existing GitHub release is
skipped. The Release workflow can also be dispatched manually from main; its
**dry_run** input defaults to true, validating without publishing. Release jobs
are serialized. If publication succeeded but release creation failed, rerun the
workflow: existing PyPI files are skipped and GitHub release creation is retried.
Do not change files for a version already uploaded to PyPI; bump the version.

## Azure account prices

Retail prices remain the default and require no login. To use negotiated
consumption rates through the logged-in Azure CLI user:

```sh
az login
bicep-cost estimate main.bicep --azure-prices --subscription-id YOUR_SUBSCRIPTION_ID
# Persist a private snapshot while estimating:
bicep-cost estimate main.bicep --azure-prices --save-price-sheet account-prices.json
# Or download separately:
bicep-cost price-sheet --subscription-id YOUR_SUBSCRIPTION_ID -o account-prices.json
# Reuse without Azure login:
bicep-cost estimate main.bicep --price-sheet account-prices.json
```

Without a subscription argument, downloads use the CLI's current subscription.
The read-only [Consumption Price Sheet API](https://learn.microsoft.com/rest/api/consumption/price-sheet/get)
requires access to the subscription's pricing and a supported billing agreement.
An ordinary Azure login alone does not guarantee access. Unsupported agreements,
missing sheets and denied access produce an error; no retail fallback occurs.

For MCA/MPA accounts, or when subscription downloads are unavailable, download
and extract the current consumption price-sheet CSV from
[Cost Management + Billing](https://learn.microsoft.com/azure/cost-management-billing/manage/ea-pricing),
then use `--price-sheet prices.csv`. The importer accepts case-insensitive column
names `meterId`, optional `skuId`, `currencyCode` or `currency`, `unitOfMeasure`,
`unitPrice`, optional `tierMinimumUnits` and `priceType`. It uses **price currency**,
not `billingCurrency`; reservations and savings-plan rows are excluded. Import a
single current price period: conflicting duplicate rates are rejected. Legacy
CSV layouts must be converted to these columns first. Included-quantity grants
are rejected because the sheet does not supply enough billing-scope information
to allocate them safely.

Account mode discovers meter identities through the Retail API, then replaces
rates by meter/SKU ID, normalizing compatible block units (for example 100 Hours
to 1 Hour). Missing account meters become unresolved; use `--strict` to block
incomplete estimates. Manual `[discounts]` are ignored because account rates
already include discounts. Reports record the sheet's origin and snapshot time.
Explicit `--currency` and usage-file currency must match the sheet; by default
its currency is selected. No exchange-rate conversion is performed.

Snapshots contain negotiated rates, not Azure tokens, and are saved atomically
with owner-only permissions. Keep them private and out of source control. Saved
sheets can be used without login; completely offline estimates also need the
Retail API metadata in the normal price cache. Use `--price-sheet FILE --offline`
for that; `--azure-prices` always downloads fresh rates. Snapshots are not refreshed
automatically, so download a new sheet when your agreement or billing period changes.
