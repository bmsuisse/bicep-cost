from bicep_cost.armexpr import Unknown
from bicep_cost.engine import DeploymentSettings, Engine


def expand(template: dict, parameters: dict | None = None) -> Engine:
    engine = Engine(DeploymentSettings())
    engine.expand(template, parameters or {})
    return engine


def test_copy_condition_and_variables():
    engine = expand(
        {
            "parameters": {
                "count": {"type": "int", "defaultValue": 3},
                "deploy": {"type": "bool", "defaultValue": True},
            },
            "variables": {"prefix": "st", "names": ["a", "b"]},
            "resources": [
                {
                    "type": "Microsoft.Storage/storageAccounts",
                    "name": "[format('{0}{1}', variables('prefix'), copyIndex())]",
                    "location": "westeurope",
                    "sku": {"name": "Standard_LRS"},
                    "copy": {"name": "loop", "count": "[parameters('count')]"},
                },
                {
                    "condition": "[not(parameters('deploy'))]",
                    "type": "Microsoft.KeyVault/vaults",
                    "name": "skipped",
                },
            ],
        }
    )
    assert [r.name for r in engine.resources] == ["st0", "st1", "st2"]


def test_property_copy_loops():
    engine = expand(
        {
            "variables": {"rules": ["x", "y"]},
            "resources": [
                {
                    "type": "Microsoft.Storage/storageAccounts",
                    "name": "s",
                    "properties": {
                        "copy": [
                            {
                                "name": "rules",
                                "count": "[length(variables('rules'))]",
                                "input": {"id": "[variables('rules')[copyIndex('rules')]]"},
                            }
                        ]
                    },
                }
            ],
        }
    )
    assert engine.resources[0].get("properties.rules") == [{"id": "x"}, {"id": "y"}]


def _module(name: str, params: dict, template: dict) -> dict:
    return {
        "type": "Microsoft.Resources/deployments",
        "name": name,
        "properties": {
            "expressionEvaluationOptions": {"scope": "inner"},
            "mode": "Incremental",
            "parameters": params,
            "template": template,
        },
    }


def test_nested_module_params_defaults_and_outputs():
    inner_a = {
        "parameters": {
            "size": {"type": "string"},
            "loc": {"type": "string", "defaultValue": "[resourceGroup().location]"},
        },
        "resources": [
            {
                "type": "Microsoft.Web/serverfarms",
                "name": "plan",
                "location": "[parameters('loc')]",
                "sku": {"name": "[parameters('size')]"},
            }
        ],
        "outputs": {"planId": {"type": "string", "value": "[resourceId('Microsoft.Web/serverfarms', 'plan')]"}},
    }
    inner_b = {
        "parameters": {"planId": {"type": "string"}},
        "resources": [
            {"type": "Microsoft.Web/sites", "name": "site", "properties": {"serverFarmId": "[parameters('planId')]"}}
        ],
    }
    engine = expand(
        {
            "resources": [
                # b is listed first and depends on a's output: evaluation must follow references, not list order
                _module("b", {"planId": {"value": "[reference('a').outputs.planId.value]"}}, inner_b),
                _module("a", {"size": {"value": "P1v3"}}, inner_a),
            ]
        }
    )
    by_type = {r.type: r for r in engine.resources if r.type != "Microsoft.Resources/deployments"}
    plan, site = by_type["Microsoft.Web/serverfarms"], by_type["Microsoft.Web/sites"]
    assert plan.get("sku.name") == "P1v3"
    assert plan.location == "westeurope"
    assert site.get("properties.serverFarmId") == plan.id
    assert plan.module_path == ("a",)


def test_parameter_entry_can_be_an_expression_yielding_value():
    inner = {
        "parameters": {"name": {"type": "string"}},
        "resources": [{"type": "Microsoft.KeyVault/vaults", "name": "[parameters('name')]"}],
    }
    engine = expand(
        {
            "parameters": {"given": {"type": "string", "defaultValue": ""}},
            "resources": [
                _module(
                    "m",
                    {
                        "name": "[if(empty(parameters('given')), createObject('value', 'generated'), createObject('value', parameters('given')))]"  # noqa: E501
                    },
                    inner,
                )
            ],
        }
    )
    assert [r.name for r in engine.resources if r.type == "Microsoft.KeyVault/vaults"] == ["generated"]


def test_unknown_name_gets_placeholder_and_warning():
    engine = expand(
        {
            "parameters": {"n": {"type": "string"}},
            "resources": [{"type": "Microsoft.KeyVault/vaults", "name": "[parameters('n')]"}],
        }
    )
    assert engine.resources[0].name.startswith("unknown-name-")
    assert engine.diagnostics


def test_reference_to_resource_in_template_returns_declared_properties():
    engine = expand(
        {
            "resources": [
                {"type": "Microsoft.Storage/storageAccounts", "name": "sa", "properties": {"accessTier": "Cool"}},
                {
                    "type": "Microsoft.Web/sites",
                    "name": "site",
                    "properties": {
                        "tier": "[reference(resourceId('Microsoft.Storage/storageAccounts', 'sa'), '2023-01-01').accessTier]",  # noqa: E501
                        "key": "[reference(resourceId('Microsoft.Storage/storageAccounts', 'sa'), '2023-01-01').primaryEndpoints]",  # noqa: E501
                    },
                },
            ]
        }
    )
    site = next(r for r in engine.resources if r.name == "site")
    assert site.get("properties.tier") == "Cool"
    assert isinstance(site.properties["key"], Unknown)


def test_subscription_scope_resource_group_override():
    inner = {
        "resources": [
            {"type": "Microsoft.Storage/storageAccounts", "name": "s", "location": "[resourceGroup().location]"}
        ],
        "outputs": {"rg": {"type": "string", "value": "[resourceGroup().name]"}},
    }
    dep = _module("m", {}, inner)
    dep["resourceGroup"] = "my-rg"
    engine = expand({"resources": [dep]})
    res = next(r for r in engine.resources if r.type == "Microsoft.Storage/storageAccounts")
    assert res.resource_group == "my-rg"
    assert "/resourceGroups/my-rg/" in res.id


def test_absurd_copy_counts_are_refused():
    engine = expand(
        {
            "resources": [
                {
                    "type": "Microsoft.Storage/storageAccounts",
                    "name": "[format('s{0}', copyIndex())]",
                    "copy": {"name": "l", "count": 1_000_000_000},
                },
                {
                    "type": "Microsoft.KeyVault/vaults",
                    "name": "kv",
                    "properties": {"copy": [{"name": "x", "count": 1_000_000_000, "input": 1}]},
                },
            ]
        }
    )
    assert [r.name for r in engine.resources] == ["kv"]
    assert engine.resources[0].get("properties.x") == []
    assert sum("exceeds the limit" in d.message for d in engine.diagnostics) == 2
