import pytest

from bicep_cost.armexpr import JsonValue, Unknown, evaluate_string, is_expression
from bicep_cost.engine import DeploymentSettings, Engine, TemplateScope


def scope(parameters: dict | None = None, variables: dict | None = None) -> TemplateScope:
    engine = Engine(DeploymentSettings())
    template: dict[str, JsonValue] = {
        "parameters": {k: {"type": "string"} for k in (parameters or {})},
        "variables": variables or {},
        "resources": [],
    }
    return TemplateScope(engine, template, parameters, (), "sub", "rg", "dep", None)


def ev(expr: str, **params):
    return evaluate_string(expr, scope(params))


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("[concat('a', 'b', string(1))]", "ab1"),
        ("[format('{0}-{1:D3}', 'x', 7)]", "x-007"),
        ("[format('{{0}}')]", "{0}"),
        ("[if(equals('A', 'a'), 'yes', 'no')]", "yes"),
        ("[createObject('a', 1, 'b', createArray(1, 2))]", {"a": 1, "b": [1, 2]}),
        ("[union(createObject('a', 1), createObject('b', 2))]", {"a": 1, "b": 2}),
        ("[length(createArray(1, 2, 3))]", 3),
        ("[div(10, 4)]", 2),
        ("[mul(3, 4)]", 12),
        ("[toLower('ABC')]", "abc"),
        ("[replace('a-b', '-', '_')]", "a_b"),
        ("[split('a,b', ',')]", ["a", "b"]),
        ("[last(split('a/b/c', '/'))]", "c"),
        ("[empty('')]", True),
        ("[not(true())]", False),
        ("[coalesce(null(), 'x')]", "x"),
        ("[createArray('a', 'b')[1]]", "b"),
        ("[createObject('k', createObject('n', 5)).k.n]", 5),
        ("[[literal]", "[literal]"),
        ("plain", "plain"),
        ("[json('{\"a\": 2}').a]", 2),
        ("[tryGet(createObject('a', 1), 'b')]", None),
        ("[contains(createObject('Key', 1), 'key')]", True),
    ],
)
def test_functions(expr, expected):
    assert ev(expr) == expected


def test_resource_id_forms():
    assert ev("[resourceId('Microsoft.Web/sites', 'app')]") == (
        "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Web/sites/app"
    )
    assert ev("[resourceId('other-rg', 'Microsoft.Web/sites/slots', 'app', 'test')]") == (
        "/subscriptions/sub/resourceGroups/other-rg/providers/Microsoft.Web/sites/app/slots/test"
    )
    assert ev("[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'x')]").startswith(
        "/subscriptions/sub/providers/"
    )
    assert ev("[extensionResourceId('/subscriptions/s/x', 'Microsoft.Authorization/roleAssignments', 'g')]") == (
        "/subscriptions/s/x/providers/Microsoft.Authorization/roleAssignments/g"
    )


def test_unknown_propagates_but_stays_opaque_in_containers():
    assert isinstance(ev("[parameters('missing')]"), Unknown)
    assert isinstance(ev("[concat('a', parameters('missing'))]"), Unknown)
    assert isinstance(ev("[if(parameters('missing'), 1, 2)]"), Unknown)
    obj = ev("[createObject('known', 1, 'unk', parameters('missing'))]")
    assert obj["known"] == 1
    assert isinstance(obj["unk"], Unknown)
    assert isinstance(ev("[listKeys('x', '2020-01-01')]"), Unknown)
    assert isinstance(ev("[reference('x').a]"), Unknown)


def test_lazy_branches_and_short_circuit():
    assert ev("[if(true(), 'ok', parameters('missing'))]") == "ok"
    assert ev("[and(false(), parameters('missing'))]") is False
    assert ev("[or(true(), parameters('missing'))]") is True


def test_is_expression():
    assert is_expression("[parameters('a')]")
    assert not is_expression("[[x]")
    assert not is_expression("abc")


def test_unique_string_is_stable_and_shaped():
    a = ev("[uniqueString('x', 'y')]")
    assert a == ev("[uniqueString('x', 'y')]")
    assert len(a) == 13


def test_uri_resolves_relative_references_and_uri_component_encodes_utf8():
    assert ev("[uri('https://host/templates/main.json', 'child.json')]") == "https://host/templates/child.json"
    assert ev("[uri('https://host/templates/', 'child.json')]") == "https://host/templates/child.json"
    assert ev("[uriComponent('café a/b')]") == "caf%C3%A9%20a%2Fb"
