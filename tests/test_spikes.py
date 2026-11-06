import io
import json
import urllib.error
from datetime import UTC, date, datetime, timedelta
from email.message import Message

import pytest

from bicep_cost import cli, spikes
from bicep_cost.collect import arm
from bicep_cost.collect.arm import AzureError
from bicep_cost.spikes import CostRow, detect_spikes, find_spikes, resolve_scope

TODAY = date(2026, 11, 1)
LAST_DAY = TODAY - timedelta(days=1)
START = LAST_DAY - timedelta(days=30)


def series(
    group: str, baseline: float, recent: float, recent_days: int = 3, today: float | None = None
) -> list[CostRow]:
    days = [START + timedelta(n) for n in range(31)]
    rows = [CostRow(group, d, baseline if i < 31 - recent_days else recent) for i, d in enumerate(days)]
    if today is not None:
        rows.append(CostRow(group, TODAY, today))
    return rows


def spike_groups(rows: list[CostRow], **kw: float) -> list[str]:
    found = find_spikes(rows, START, LAST_DAY, int(kw.get("recent", 3)), kw.get("min_delta", 5.0), kw.get("ratio", 1.5))
    return [s.group for s in found]


def test_flat_group_is_not_flagged() -> None:
    assert spike_groups(series("Storage", 10, 10)) == []


def test_thirty_fold_spike_is_flagged_with_numbers() -> None:
    (found,) = find_spikes(series("Storage", 10, 300), START, LAST_DAY, 3, 5.0, 1.5)
    assert (found.group, found.baseline_per_day, found.recent_per_day, found.delta_per_day) == ("Storage", 10, 300, 290)
    assert found.ratio == 30.0
    assert not found.new
    assert len(found.daily) == 10
    assert list(found.daily.values())[-3:] == [300, 300, 300]


def test_new_group_without_baseline_is_flagged_as_new() -> None:
    days = [LAST_DAY - timedelta(n) for n in range(3)]
    rows = [CostRow("Fabric", d, 40.0) for d in days]
    (found,) = find_spikes(rows, START, LAST_DAY, 3, 5.0, 1.5)
    assert found.new
    assert found.ratio is None
    assert found.baseline_per_day == 0


def test_todays_partial_day_is_ignored() -> None:
    assert spike_groups(series("Storage", 10, 10, today=5000)) == []
    (found,) = find_spikes(series("Storage", 10, 300, today=0), START, LAST_DAY, 3, 5.0, 1.5)
    assert found.recent_per_day == 300


def test_ratio_threshold() -> None:
    assert spike_groups(series("a", 100, 140)) == []  # +40/day but only 1.4x
    assert spike_groups(series("a", 100, 150)) == ["a"]


def test_min_delta_threshold() -> None:
    assert spike_groups(series("a", 1, 4)) == []  # 4x but only +3/day
    assert spike_groups(series("a", 1, 4), min_delta=3) == ["a"]


def test_sparse_baseline_with_zero_median_does_not_divide_by_zero() -> None:
    rows = [CostRow("a", START + timedelta(2), 3.0), *[CostRow("a", LAST_DAY - timedelta(n), 50.0) for n in range(3)]]
    (found,) = find_spikes(rows, START, LAST_DAY, 3, 5.0, 1.5)
    assert found.ratio is None
    assert not found.new


def test_sorted_by_absolute_delta_and_empty_result() -> None:
    rows = series("small", 10, 40) + series("big", 100, 400) + series("ratio-only", 1, 4)
    assert spike_groups(rows) == ["big", "small"]
    assert spike_groups([]) == []


def page(rows: list[list[object]], next_link: str | None = None, group: str = "ServiceName") -> dict[str, object]:
    cols = [{"name": n} for n in ("Cost", "UsageDate", group, "Currency")]
    return {"properties": {"columns": cols, "rows": rows, "nextLink": next_link}}


class Recorder:
    def __init__(self, *pages: dict[str, object] | None) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []

    def __call__(self, method: str, url: str, body: dict[str, object] | None) -> dict[str, object] | None:
        self.calls.append((method, url, body))
        return self.pages.pop(0)


def test_request_body_is_custom_timeframe_single_grouping_without_filter() -> None:
    rec = Recorder(page([]))
    detect_spikes(rec, scope="/subscriptions/s", today=TODAY, days=31)
    method, url, body = rec.calls[0]
    assert method == "POST"
    assert url == (
        "https://management.azure.com/subscriptions/s/providers/Microsoft.CostManagement/query?api-version=2023-11-01"
    )
    assert body is not None
    assert body["type"] == "ActualCost"
    assert body["timeframe"] == "Custom"
    assert body["timePeriod"] == {"from": START.isoformat(), "to": TODAY.isoformat()}
    dataset = body["dataset"]
    assert isinstance(dataset, dict)
    assert dataset["granularity"] == "Daily"
    assert dataset["grouping"] == [{"type": "Dimension", "name": "ServiceName"}]
    assert "filter" not in dataset


def test_service_filter_uses_singular_operator_key() -> None:
    rec = Recorder(page([], group="ResourceId"))
    detect_spikes(rec, scope="/subscriptions/s", today=TODAY, group_by="ResourceId", service="Storage")
    body = rec.calls[0][2]
    assert body is not None
    dataset = body["dataset"]
    assert isinstance(dataset, dict)
    assert dataset["grouping"] == [{"type": "Dimension", "name": "ResourceId"}]
    assert dataset["filter"] == {"dimensions": {"name": "ServiceName", "operator": "In", "values": ["Storage"]}}


def test_rows_are_mapped_by_column_and_nextlink_is_followed() -> None:
    sleeps: list[float] = []
    day = int(LAST_DAY.strftime("%Y%m%d"))
    rec = Recorder(
        page([[500.0, day, "Storage", "CHF"]], next_link="https://next"),
        page([[7.0, day, "", "CHF"]]),
    )
    report = detect_spikes(rec, scope="/subscriptions/s", today=TODAY, recent=1, days=3, sleep=sleeps.append)
    assert [(c[0], c[1]) for c in rec.calls] == [
        ("POST", rec.calls[0][1]),
        ("POST", "https://next"),
    ]
    assert rec.calls[1][2] == rec.calls[0][2]
    assert sleeps == [spikes.PAGE_PAUSE]
    assert report.currency == "CHF"
    assert [s.group for s in report.spikes] == ["Storage", "(none)"]
    assert report.total_per_day == {LAST_DAY: 507.0}


def test_missing_column_and_empty_payload_raise() -> None:
    with pytest.raises(AzureError, match="no result"):
        detect_spikes(Recorder(None), scope="/subscriptions/s", today=TODAY)
    no_day: dict[str, object] = {"properties": {"columns": [{"name": "Cost"}, {"name": "ServiceName"}], "rows": []}}
    with pytest.raises(AzureError, match="UsageDate"):
        detect_spikes(Recorder(no_day), scope="/subscriptions/s", today=TODAY)


@pytest.mark.parametrize(("days", "recent"), [(32, 3), (1, 1), (10, 10), (10, 0)])
def test_window_is_validated(days: int, recent: int) -> None:
    with pytest.raises(ValueError, match="--days"):
        detect_spikes(Recorder(), scope="/subscriptions/s", today=TODAY, days=days, recent=recent)


def test_resolve_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spikes, "az_current_subscription", lambda: "sub-az")
    assert resolve_scope("/subscriptions/x/resourceGroups/rg/", None, None) == "/subscriptions/x/resourceGroups/rg"
    assert resolve_scope(None, None, None) == "/subscriptions/sub-az"
    assert resolve_scope(None, "s1", "rg") == "/subscriptions/s1/resourceGroups/rg"
    assert resolve_scope(None, None, "rg") == "/subscriptions/sub-az/resourceGroups/rg"


def http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = Message()
    if retry_after:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://management.azure.com/x", code, "err", headers, io.BytesIO(b"nope"))


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def patch_urlopen(monkeypatch: pytest.MonkeyPatch, outcomes: list[Exception | dict[str, object]]) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(arm.time, "sleep", sleeps.append)

    def fake(*_: object, **__: object) -> FakeResponse:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)

    monkeypatch.setattr(arm.urllib.request, "urlopen", fake)
    return sleeps


def test_429_is_retried_after_15_30_60_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = patch_urlopen(monkeypatch, [http_error(429), http_error(429), http_error(429), {"ok": True}])
    transport = arm.http_transport(lambda: "token", throttle_waits=spikes.RETRY_WAITS)
    assert transport("POST", "https://management.azure.com/x", {}) == {"ok": True}
    assert sleeps == [15, 30, 60]


def test_429_wait_is_at_least_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = patch_urlopen(monkeypatch, [http_error(429, "40"), {"ok": True}])
    arm.http_transport(lambda: "token", throttle_waits=spikes.RETRY_WAITS)("POST", "https://management.azure.com/x", {})
    assert sleeps == [40]


def test_429_gives_up_after_three_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = patch_urlopen(monkeypatch, [http_error(429) for _ in range(4)])
    transport = arm.http_transport(lambda: "token", throttle_waits=spikes.RETRY_WAITS)
    with pytest.raises(AzureError, match="429"):
        transport("POST", "https://management.azure.com/x", {})
    assert sleeps == [15, 30, 60]


def test_default_transport_keeps_short_backoff_for_429(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = patch_urlopen(monkeypatch, [http_error(429), {"ok": True}])
    assert arm.http_transport(lambda: "token")("GET", "https://management.azure.com/x", None) == {"ok": True}
    assert sleeps == [1]


def test_spikes_command_prints_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    seen: dict[str, object] = {}

    def fake_transport(**kwargs: object) -> Recorder:
        seen.update(kwargs)
        day = int((datetime.now(UTC).date() - timedelta(days=1)).strftime("%Y%m%d"))
        return Recorder(page([[900.0, day, "Storage", "CHF"]]))

    monkeypatch.setattr(cli, "http_transport", fake_transport)
    monkeypatch.setattr(cli, "az_current_subscription", lambda: "unused")
    monkeypatch.setattr(spikes, "az_current_subscription", lambda: "sub-az")
    assert cli.main(["spikes", "--json", "--days", "5", "--recent", "1", "--resource-group", "rg"]) == 0
    assert seen == {"throttle_waits": spikes.RETRY_WAITS}
    out = json.loads(capsys.readouterr().out)
    assert out["scope"] == "/subscriptions/sub-az/resourceGroups/rg"
    assert out["currency"] == "CHF"
    assert [s["ServiceName"] for s in out["spikes"]] == ["Storage"]
    assert out["spikes"][0]["new"] is True


def test_spikes_command_validates_window_before_touching_azure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_azure(**_: object) -> None:
        raise AssertionError("must not authenticate for an invalid window")

    monkeypatch.setattr(cli, "http_transport", no_azure)
    assert cli.main(["spikes", "--scope", "/subscriptions/s", "--days", "40"]) == 1
    assert "--days" in capsys.readouterr().err


def test_credits_in_the_baseline_do_not_make_a_group_new() -> None:
    base = [100.0] * 27 + [-2700.0]  # sums to zero, median 100
    rows = [CostRow("a", START + timedelta(n), c) for n, c in enumerate(base)]
    rows += [CostRow("a", LAST_DAY - timedelta(n), 110.0) for n in range(3)]
    assert spike_groups(rows, min_delta=1) == []


def test_min_delta_boundary_is_not_lost_to_float_rounding() -> None:
    assert spike_groups(series("a", 5.2, 10.2)) == ["a"]


def test_text_output_keeps_full_resource_ids() -> None:
    rid = "/subscriptions/71dea60e/resourceGroups/rg-data/providers/Microsoft.Storage/storageAccounts/stprod"
    (found,) = find_spikes(series(rid, 10, 300), START, LAST_DAY, 3, 5.0, 1.5)
    report = spikes.SpikeReport("/s", "EUR", "ActualCost", "ResourceId", START, LAST_DAY, 3, [found], {})
    assert rid in spikes.render_text(report)


def test_empty_success_body_is_no_result_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    class Empty:
        def __enter__(self) -> "Empty":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return b""

    monkeypatch.setattr(arm.urllib.request, "urlopen", lambda *_, **__: Empty())
    assert arm.http_transport(lambda: "token")("GET", "https://management.azure.com/x", None) is None
