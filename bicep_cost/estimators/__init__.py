"""Estimator registry: ARM resource type (lower case) -> function pricing one resource."""

from collections.abc import Callable

from ..context import EstimateContext
from ..engine import Resource
from ..model import ResourceEstimate
from . import aca, appservice, keyvault, monitor, network, postgres, servicebus, sql, storage, vm
from .common import free

Estimator = Callable[[Resource, EstimateContext], ResourceEstimate]

ESTIMATORS: dict[str, Estimator] = {
    "microsoft.web/serverfarms": appservice.estimate_plan,
    "microsoft.web/sites": appservice.estimate_site,
    "microsoft.app/managedenvironments": aca.estimate_environment,
    "microsoft.app/containerapps": aca.estimate_container_app,
    "microsoft.app/sessionpools": aca.estimate_session_pool,
    "microsoft.storage/storageaccounts": storage.estimate_account,
    "microsoft.storage/storageaccounts/fileservices/shares": storage.estimate_share,
    "microsoft.dbforpostgresql/flexibleservers": postgres.estimate_server,
    "microsoft.operationalinsights/workspaces": monitor.estimate_workspace,
    "microsoft.insights/components": monitor.estimate_app_insights,
    "microsoft.insights/scheduledqueryrules": monitor.estimate_log_alert,
    "microsoft.insights/diagnosticsettings": monitor.estimate_diagnostic_setting,
    "microsoft.keyvault/vaults": keyvault.estimate_vault,
    "microsoft.cognitiveservices/accounts": keyvault.estimate_cognitive_account,
    "microsoft.servicebus/namespaces": servicebus.estimate_namespace,
    "microsoft.sql/servers/databases": sql.estimate_database,
    "microsoft.sql/servers/elasticpools": sql.estimate_elastic_pool,
    "microsoft.sql/managedinstances": sql.estimate_managed_instance,
    "microsoft.compute/virtualmachines": vm.estimate_vm,
    "microsoft.compute/virtualmachinescalesets": vm.estimate_vmss,
    "microsoft.compute/disks": vm.estimate_disk,
    "microsoft.network/publicipaddresses": vm.estimate_public_ip,
    "microsoft.network/privateendpoints": network.estimate_private_endpoint,
    "microsoft.network/privatednszones": network.estimate_private_dns_zone,
}

# Resource types that carry no charge of their own.
FREE_TYPES: dict[str, str] = {
    "microsoft.resources/resourcegroups": "resource groups are free",
    "microsoft.resources/deployments": "deployments are free",
    "microsoft.authorization/roleassignments": "role assignments are free",
    "microsoft.authorization/roledefinitions": "role definitions are free",
    "microsoft.managedidentity/userassignedidentities": "managed identities are free",
    "microsoft.network/virtualnetworks": "virtual networks are free",
    "microsoft.network/virtualnetworks/subnets": "subnets are free",
    "microsoft.network/virtualnetworks/virtualnetworkpeerings": "peering itself is free (data transfer is billed)",
    "microsoft.network/networksecuritygroups": "NSGs are free",
    "microsoft.network/routetables": "route tables are free",
    "microsoft.network/networkinterfaces": "network interfaces are free",
    "microsoft.network/privatednszones/virtualnetworklinks": "VNet links are free",
    "microsoft.network/privatednszones/a": "DNS records are free",
    "microsoft.network/privateendpoints/privatednszonegroups": "DNS zone groups are free",
    "microsoft.portal/dashboards": "dashboards are free",
    "microsoft.consumption/budgets": "budgets are free",
    "microsoft.insights/actiongroups": "action groups are free (SMS/voice notifications are billed per message)",
    "microsoft.keyvault/vaults/accesspolicies": "access policies are free",
    "microsoft.keyvault/vaults/secrets": "secrets are billed via vault operations",
    "microsoft.keyvault/vaults/keys": "software keys are billed via vault operations",
    "microsoft.app/containerapps/authconfigs": "auth config is free",
    "microsoft.app/managedenvironments/storages": "environment storage mounts are free",
    "microsoft.dbforpostgresql/flexibleservers/databases": "databases are included in the server price",
    "microsoft.dbforpostgresql/flexibleservers/configurations": "server parameters are free",
    "microsoft.dbforpostgresql/flexibleservers/firewallrules": "firewall rules are free",
    "microsoft.dbforpostgresql/flexibleservers/administrators": "Entra admins are free",
    "microsoft.storage/storageaccounts/managementpolicies": "lifecycle management policies are free",
    "microsoft.sql/servers": "the logical SQL server is free; its databases and pools are priced",
    "microsoft.compute/availabilitysets": "availability sets are free",
    "microsoft.compute/proximityplacementgroups": "placement groups are free",
    "microsoft.compute/sshpublickeys": "SSH keys are free",
    "microsoft.compute/diskencryptionsets": "disk encryption sets are free",
    "microsoft.devtestlab/schedules": "auto-shutdown schedules are free",
}

# Child resources of a billed parent that are never billed separately.
FREE_PREFIXES: dict[str, str] = {
    "microsoft.web/sites/": "site configuration, slots and bindings run on the plan",
    "microsoft.storage/storageaccounts/": "services and containers are billed through the account's usage",
    "microsoft.servicebus/namespaces/": "queues, topics, subscriptions and rules are included in the namespace price",
    "microsoft.sql/servers/": "server settings are free; Defender for SQL is billed separately",
    "microsoft.sql/managedinstances/": "managed instance databases and settings are included in the instance price",
    "microsoft.compute/virtualmachines/": "VM extensions are free",
    "microsoft.compute/virtualmachinescalesets/": "scale set extensions are free",
}


def estimator_for(type_: str) -> Estimator | None:
    low = type_.lower()
    if low in ESTIMATORS:
        return ESTIMATORS[low]
    if low in FREE_TYPES:
        reason = FREE_TYPES[low]
        return lambda res, ctx: free(res, reason)
    for prefix, reason in FREE_PREFIXES.items():
        if low.startswith(prefix):
            return lambda res, ctx, reason=reason: free(res, reason)
    return None
