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

### Resources the template only references

A template that just *references* platform-owned resources (`resource plan '...' existing = {...}`: App Service plan,
database, Log Analytics, ...) deploys none of them, so they cost nothing in the estimate and a budget cap guards
nothing. Describe them in a JSON file and pass it with `--existing`:

```bash
bicep-cost estimate main.bicep -p parameters/prod.bicepparam --existing infra/existing.json
bicep-cost collect  main.bicep -p parameters/prod.bicepparam --existing infra/existing.json --resource-group <rg>
```

The file is a list (or `{"resources": [...]}`) of resources with the same fields as in an ARM template - `type`, `name`,
`location`, `kind`, `sku`, `properties` - see [`examples/existing.json`](examples/existing.json). They are priced by the
estimator of their type and take usage values like any other resource (`collect` measures them and reports drift
against the file). `resourceGroup` / `subscriptionId` default to the deployment's, so `plan.id` in the template
matches the entry; set them for resources living elsewhere. Sites and slots then run on the listed plan instead of
being reported as *external*. The option is repeatable (later files win), and a resource the template deploys itself
always takes precedence over an entry for the same id. Use only the properties an estimator reads (SKU, capacity,
storage size, ...); there is no need to dump the full resource.

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
| Functions / Logic Apps Standard | `serverfarms` + `sites` | Consumption (Y1), Flex (FC1: always-ready + on-demand), Elastic Premium EP1-3, Logic Apps Standard WS1-3 (managed connector calls via `connector_actions_per_month`, built-in operations are free) |
| Logic Apps Consumption | `Microsoft.Logic/workflows`, `integrationAccounts` | Priced from the workflow definition: trigger polls from the `Recurrence`, actions per run (loops, built-in / standard / enterprise connector classes, the first 4,000 built-in operations per subscription free), run history retention; integration accounts Free / Basic / Standard. Disabled workflows are free, ISE workflows are *external*. Retries and the share of branches that actually run are not modelled |
| Kubernetes | `Microsoft.ContainerService/managedClusters`, `agentPools` | Free / Standard / Premium tier fee; node pool VMs (any size, Linux / Windows, Spot, Hybrid Benefit) with managed OS disks (ephemeral = free); autoscaled pools at `minCount`. AKS Automatic: hosted control plane fee only. **Not** load balancers, public IPs, egress, persistent volumes, add-ons |
| Container Apps | `managedEnvironments`, `containerApps`, `sessionPools` | Consumption profile (active/idle rates, free grants, KEDA cron schedules), dedicated profiles D4-D32 / E4-E32 (+ plan management fee), dynamic sessions (Python/Node) and custom-container pools |
| Storage | `storageAccounts`, file `shares` | LRS / ZRS / GRS / RA-GRS / GZRS / RA-GZRS; Hot / Cool / Cold; Blob, ADLS Gen2 (HNS), standard file shares (TransactionOptimized / Hot / Cool). **Not** Premium |
| PostgreSQL | `flexibleServers` | Burstable, General Purpose, Memory Optimized (any series); HA doubles compute + storage; Premium SSD v2 IOPS / throughput; backup storage above the free allowance (geo-redundant aware) |
| Azure SQL | `servers/databases`, `servers/elasticPools`, `managedInstances` | vCore: General Purpose (provisioned and serverless), Business Critical, Hyperscale (+ HA replicas), zone redundancy, Gen5 / DC / Fsv2 / M families; DTU: Basic, Standard S0-S12, Premium P1-P15; DTU elastic pools; Managed Instance GP / BC. Databases in a pool are included. Usage keys use the full name `server/database`. Azure Hybrid Benefit is not applied (price shown includes the license). Live SKUs as returned by Azure (`Standard` + capacity, `GP_S_Gen5` + capacity) are understood too. **Not** Synapse dedicated pools, LTR backups |
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

## Scan a deployed subscription

No template needed: `scan` lists what is deployed with Azure Resource Graph and prices it with the same estimators.

```bash
az login
bicep-cost scan                                   # current subscription, list prices, default usage assumptions
bicep-cost scan --actuals                         # + compare with the Cost Management bill per resource type
bicep-cost scan --measure --days 30 --save-usage prod.usage.toml   # + measure usage from Azure Monitor (implies --actuals)
bicep-cost scan --subscription-id <id> --resource-group rg-a --resource-group rg-b --format markdown
```

Everything is read-only (Resource Graph and Cost Management queries, Azure Monitor / Log Analytics reads). Needs Reader;
`--actuals` also Cost Management Reader.

With `--actuals` the report ends with a **coverage table** per resource type: what the estimators priced (*modelled*)
against the actual cost of the window scaled to a month, how many resources had to be priced from the bill, and the
cost of resource ids that are no longer in the inventory (deleted, or short-lived nodes of managed services):

- Resource types **without an estimator** (or that could not be priced) are priced from their actual cost instead of
  being dropped, so the total stays complete. Never done without `--actuals`.
- Resources that belong to a managed service (`managedBy`, e.g. Databricks cluster nodes) come and go with the
  workload; they are priced from their actual cost instead of a 24/7 assumption.
- *Modelled / actual* far from 100% points at a gap: usage defaults for storage / logs / AI (use `--measure`),
  negotiated discounts or reservations (use `--azure-prices` or `[discounts]`), or a modelling error worth an issue.
- The estimate uses the billing currency of the actuals; `--currency` must match, no conversion happens.

`--baseline` / `--fail-above` / `--format json` work as for `estimate`; the JSON gains a `coverage` object.
Resource Graph does not list child resources such as file shares and lags a few minutes behind ARM. Cost Management
allows few queries per minute; throttled calls are retried automatically.

## Other commands and flags

```bash
bicep-cost estimate ... --format json -o head.json     # also: markdown (PR comments), csv
bicep-cost diff base.json head.json                    # per-resource cost change between two reports
bicep-cost estimate ... --fail-above 1500              # exit 3 above a monthly budget (CI guard rail)
bicep-cost estimate ... --strict                       # exit 2 if something could not be priced
bicep-cost estimate ... --group-by type|module         # summary table instead of per-resource rows
bicep-cost prices --service "Azure Container Apps" --meter-contains Idle   # browse meters
bicep-cost spikes                                      # Azure cost spikes of the last days (needs az login)
bicep-cost rightsize                                   # over- / under-provisioned resources from Azure Monitor (needs az login)
bicep-cost rightsize --only over --min-saving 50       # filter: --only over|under, --min-saving, --fail-on over|under
```

Other options: `--existing FILE.json` (price referenced `existing` resources), `--location` (default region and `resourceGroup().location`, default `westeurope`), `--resource-group`,
`--subscription-id`, `--offline` / `--refresh` / `--no-cache`, `-v` (log API calls).

## Find cost spikes

```bash
az login
bicep-cost spikes                                          # current az subscription, grouped by ServiceName
bicep-cost spikes --resource-group rg-prod                 # narrow the scope
bicep-cost spikes --group-by ResourceId --service Storage  # drill into one service
bicep-cost spikes --days 14 --recent 2 --min-delta 20 --ratio 2 --json
```

`spikes` answers "why did the bill jump?" from actual billing data. It runs **one** Daily Cost Management query over
the last `--days` days (default and maximum 31, the Daily ceiling), grouped by one dimension (`--group-by`, default
`ServiceName`; `ResourceGroupName`, `ResourceId`, `MeterCategory` ... work too, `ResourceId` needs a subscription or
resource group scope). For every group it compares the mean daily cost of the last `--recent` complete days (default 3)
with the median of the days before. Today is excluded because its cost data is partial. A group is flagged when:

- the increase is at least `--min-delta` per day in the billing currency (default 5), **and**
- the recent mean is at least `--ratio` times the baseline median (default 1.5), or the group had no cost at all in the
  baseline (shown as `new`; `n/a` if the median is zero but there were a few cost days).

Flagged groups are sorted by the absolute increase per day. `--json` prints the spikes with their last days of daily
cost plus the total per day. Other options: `--cost-type ActualCost|AmortizedCost`, `--scope` (any full scope path,
instead of `--subscription-id` / `--resource-group`).

`spikes` is **read-only** (a single POST to the Cost Management `query` API) and needs **Cost Management Reader** on
the scope. The Query API allows about 4 requests per minute per scope, so a 429 is retried after 15, 30 and 60 seconds
(at most 3 times) before the command fails; avoid running it in a loop.

## Find over- and under-provisioned resources

```bash
az login
bicep-cost rightsize                                       # current az subscription, 30 days of hourly metrics
bicep-cost rightsize --resource-group rg-prod --resource-group rg-data
bicep-cost rightsize --only over --min-saving 50           # only shrink proposals worth at least 50 a month
bicep-cost rightsize --days 60 --format json -o rightsize.json
bicep-cost rightsize --fail-on under                       # CI: exit 3 when something is under-provisioned
```

`rightsize` answers "which resources are too big or too small?" from **real data**, and proposes a change only when it
is sure. It lists the resources with Azure Resource Graph, reads Azure Monitor metrics at `PT1H` granularity over
`--days` days (default 30, 14 to 90) and, per metric, reports the p95 of the hourly maxima, the maximum, the mean and
the **data coverage** (hours that came back versus expected). Azure Advisor's Cost and Performance recommendations are
matched by resource id and shown next to the numbers as a second opinion. Every resource gets one verdict:

| Verdict | Meaning |
|---|---|
| `over` | low load on every sizing metric and a smaller step of the same family stays comfortable and is cheaper: a proposal |
| `under` | sustained saturation confirmed by an independent second signal: a proposal when one larger step fixes it |
| `watch` | something is off but not enough to propose (one signal only, thin or contradictory data, no price) |
| `ok` | nothing to do, or nothing that can safely be done (shown with the reason) |
| `no-data` | Azure Monitor returned nothing usable (for example an app that never ran) |

A proposal contains the target size, the monthly saving or increase at list price (from the same estimators and price
lookup as `estimate`; `--azure-prices` / `--price-sheet` work too), the predicted utilisation after the change, the risk
and the exact command that rolls it back. Proposals are ranked by absolute monthly money, increases and savings alike.
Example (synthetic data):

```
bicep-cost rightsize: subscription 00000000-0000-0000-0000-000000000000
window 30 d ending 2026-11-06 10:00 UTC (720 hourly points); prices: Azure Retail Prices API list price, pay-as-you-go (USD)
verdicts: 2 over, 1 under, 1 watch, 0 ok, 0 no-data
over proposals would save 157.53 USD/month; under proposals would cost 43.58 USD/month more

PROPOSALS (ranked by monthly money)
 1. OVER  plan-api-example  [Microsoft.Web/serverFarms, rg-example]  P2v3 x1
    why: CpuPercentage p95 11% / max 31%; MemoryPercentage p95 24% / max 26%; P1v3 x1 would reach MemoryPercentage p95 49% / max 52%
    proposal: P2v3 -> P1v3  (259.88 USD -> 129.94 USD/month, saves 129.94 USD/month, list price)
    predicted after the change: CpuPercentage p95 22% / max 62%; MemoryPercentage p95 49% / max 52%
    risk: low: the plan's workers restart while it scales (a short disruption); check memory-heavy apps afterwards
    rollback: az appservice plan update -g rg-example -n plan-api-example --sku P2V3
    CpuPercentage                    p95   10.8%  max   31.0%  mean    2.5%  720/720 h (100%)
    MemoryPercentage                 p95   24.4%  max   26.0%  mean   19.8%  720/720 h (100%)
    HttpQueueLength                  p95       0  max       0  mean       0  720/720 h (100%)
    note: 1 instance(s)
 2. UNDER pg-orders-example  [Microsoft.DBforPostgreSQL/flexibleServers, rg-example]  B1ms
    why: cpu_percent p95 100% / max 100% and connections at 90% of max_connections in 720 h; CPU credits exhausted in 80 h
    proposal: B1ms -> B2s  (32.05 USD -> 75.63 USD/month, costs 43.58 USD/month, list price)
    predicted after the change: cpu_percent p95 50% / max 50%; memory_percent p95 34% / max 37%
    risk: medium: changing the compute size restarts the server (minutes of downtime)
    rollback: az postgres flexible-server update -g rg-example -n pg-orders-example --sku-name Standard_B1ms --tier Burstable
    cpu_percent                      p95  100.0%  max  100.0%  mean   38.7%  720/720 h (100%)
    memory_percent                   p95   68.5%  max   74.7%  mean   54.0%  720/720 h (100%)
    storage_percent                  p95   13.0%  max   13.0%  mean   13.0%  720/720 h (100%)
    connections of max_connections   p95   98.0%  max   98.0%  mean   98.0%  720/720 h (100%)
    signal: connections at 90% of max_connections in 720 h
    signal: CPU credits exhausted in 80 h
 3. OVER  app-web-example  [Microsoft.App/containerApps, rg-example]  1 vCPU / 2 GiB, 1-3 replicas
    why: CpuPercentage p95 21% / max 27%; MemoryPercentage p95 35% / max 39%; 0.75 vCPU / 1.5 GiB would reach MemoryPercentage p95 47% / max 52%
    proposal: 1 vCPU / 2 GiB -> 0.75 vCPU / 1.5 GiB  (110.38 USD -> 82.78 USD/month, saves 27.59 USD/month, list price)
    predicted after the change: CpuPercentage p95 28% / max 36%; MemoryPercentage p95 47% / max 52%
    risk: low: creates a new revision; a replica that needs more memory than the new size gets OOM-killed
    rollback: az containerapp update -g rg-example -n app-web-example --cpu 1 --memory 2Gi
    CpuPercentage                    p95   20.7%  max   27.2%  mean    5.6%  720/720 h (100%)
    MemoryPercentage                 p95   35.0%  max   39.2%  mean   27.0%  720/720 h (100%)
    Replicas                         p95       1  max       1  mean       1  720/720 h (100%)
    RestartCount                     p95       0  max       0  mean       0  720/720 h (100%)
    note: configured 1-3 replicas, observed 1-1 (mean 1.0); 1 vCPU / 2 GiB per replica
    note: ran in 720 of 720 h

WATCH (not sure enough to propose): 1
  plan-batch-example  [Microsoft.Web/serverFarms, rg-example]  P1v3 x1: CpuPercentage p95 86% / max 100%, but no second signal (restarts, pinned replicas, limits, Advisor)
    CpuPercentage                    p95   85.7%  max  100.0%  mean   23.8%  720/720 h (100%)
    MemoryPercentage                 p95   62.4%  max   68.4%  mean   49.4%  720/720 h (100%)
    HttpQueueLength                  p95       0  max       0  mean       0  720/720 h (100%)
    note: 1 instance(s)

NOT COVERED (no right-sizing rules; never guessed): microsoft.compute/virtualmachines x1, microsoft.storage/storageaccounts x1
```

### When it is sure

| Rule | Threshold |
|---|---|
| Data needed for any verdict but `watch` / `no-data` | >= 90% of the expected hourly points for every sizing metric, window >= 14 days |
| `over`: every sizing metric (CPU **and** memory, or DTU, workers, IO) | p95 of the hourly maxima <= 40% **and** max <= 60% |
| `over`: the smaller step must keep the *predicted* load (current x old size / new size) | p95 <= 70% **and** max <= 85% |
| `over`: window | >= 30 days, shorter windows cannot rule out a monthly cycle (`watch`) |
| `over`: never | Burstable, serverless, Consumption / Flex, elastic or autoscale-managed resources, read replicas, resources with a contradicting signal or Advisor Performance recommendation, and anything that cannot be priced (`watch`) |
| `over`: replicas / instances | one fewer, only if the minimum stays >= 2 for zone-redundant (or unknown) setups; Container Apps only when replicas never scaled beyond the minimum |
| `under`: saturation | p95 of the hourly maxima >= 85%, **or** at least 2% of the hours at >= 95% (a single spike does not count) |
| `under`: second signal (at least one) | restarts, replicas at the maximum for >= 25% of the hours, connections >= 90% of `max_connections` or exhausted CPU credits (PostgreSQL), worker threads or a DTU component at the limit (SQL), HTTP queue >= 10 (App Service), or an Advisor Performance recommendation |
| `under`: proposal | the smallest larger step of the same family whose predicted load is <= 70% / 85%; none fits: `under` without a proposal |
| Anything between the thresholds | `ok`; saturation without a second signal: `watch` |

Storage is shown but never judged or shrunk, and a smaller size is only proposed within the same family (App Service
P0v3..P3v3, PostgreSQL D / E series, SQL DTU levels and Gen5 vCores that still hold the configured maximum size,
Container Apps Consumption sizes). A saturated but mostly idle resource (mean < 25%) is flagged as bursty.

### What is covered, and what is not

| Type | Metrics | Smaller / larger steps |
|---|---|---|
| App Service plans (`Microsoft.Web/serverFarms`) | `CpuPercentage`, `MemoryPercentage`, `HttpQueueLength` | SKU within P0v3..P3v3, P1mv3..P5mv3, P1v2..P3v2, S1..S3, B1..B3; instance count |
| PostgreSQL flexible servers | `cpu_percent`, `memory_percent`, `storage_percent`, `active_connections`, `max_connections`, `cpu_credits_remaining` | vCores within the D / E series, Burstable sizes (up only) |
| Azure SQL databases | `dtu_consumption_percent`, `cpu_percent`, `workers_percent`, `physical_data_read_percent`, `log_write_percent`, `storage_percent` | DTU levels, Gen5 vCores (provisioned General Purpose / Business Critical) |
| Container Apps (Consumption) | `Replicas`, `CpuPercentage`, `MemoryPercentage`, `RestartCount` | vCPU / memory step, minimum replicas |

Everything else is listed under *not covered* with its count, never guessed: for example virtual machines (no
reliable guest memory metric), elastic pools, Hyperscale, managed instances, AKS, Container Apps on a dedicated
workload profile (billed on the environment, so no saving can be shown), and apps with several containers (the split of
a smaller size between them is unknown). Azure SQL memory is not judged because SQL Server fills its buffer pool
whatever the load. Resources of a covered type that cannot be assessed (system databases, pool members, read replicas,
Consumption plans) are listed under *not assessed* with the reason. The JSON report (`--format json`) carries the same
evidence: window, expected and covered hours, p95 / max / mean per metric, signals, Advisor matches, thresholds and
the proposal.

`rightsize` is **read-only**: it reads Resource Graph, Azure Monitor metrics and Azure Advisor and never changes
anything. Needs `az login` with **Reader** and **Monitoring Reader** on the resources (Advisor read access for the
second opinion; without it a warning is printed and the verdicts rely on the metrics alone). About one metrics call per
resource is made, `--workers` (default 4) limits the parallelism, and throttled calls (429) are retried after 15, 30 and
60 seconds like for `spikes`. Exit code 0, except real errors (1) and `--fail-on over|under` (3).

## Development

```bash
uv sync --group test --group dev
uv run -m pytest
uv run ruff check . && uv run ruff format --check .
uv run ty check --error-on-warning
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
missing sheets and denied access produce an error. Within a successfully loaded
price sheet, missing meter rates fall back to retail prices with warnings.

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
to 1 Hour). Missing account meter rates use retail prices with an explicit pricing
warning in text, Markdown, and JSON reports; `--strict` does not block this
fallback. Resources with no retail meter still become unresolved. Manual `[discounts]` are ignored because account rates
already include discounts. Reports record the sheet's origin and snapshot time.
Explicit `--currency` and usage-file currency must match the sheet; by default
its currency is selected. No exchange-rate conversion is performed.

Snapshots contain negotiated rates, not Azure tokens, and are saved atomically
with owner-only permissions. Keep them private and out of source control. Saved
sheets can be used without login; completely offline estimates also need the
Retail API metadata in the normal price cache. Use `--price-sheet FILE --offline`
for that; `--azure-prices` always downloads fresh rates. Snapshots are not refreshed
automatically, so download a new sheet when your agreement or billing period changes.

## Quality gates

Ruff uses the strict BMS Python rule families from `init-app-stack` (annotations,
modern syntax, imports, bugbear, performance, pathlib, naming and logging).
The target stays **Python 3.11**, matching package support. FastAPI-only rules do
not apply to this CLI; CLI printing is allowed specifically in `cli.py` and the
file-size guard. Relative imports are banned. `ANN401` (explicit `Any`) is a
nonblocking warning, matching the BMS scaffold. Production and pytest function
signatures are annotated; no test-wide annotation exception is used.

Ty checks fail on warnings, with possibly missing attributes/imports and missing
generic type arguments treated as errors. Default type diagnostics remain active;
optional experimental rules are not enabled indiscriminately. CI runs the same
checks and formatting verification on Python 3.11 and 3.14.

```sh
uv sync --locked --all-groups
uv run prek install             # enable the pre-commit hook
uv run prek run --all-files      # format, lint, typecheck, YAML and file-size checks
```

The hooks use the repository's locked tool versions. Re-run after automatic fixes
and stage the resulting changes before committing. Pytest remains a CI check;
run focused tests locally for behavior you change.
