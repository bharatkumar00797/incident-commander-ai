from __future__ import annotations

from datetime import timedelta

from incident_commander.correlate import correlate
from incident_commander.topology import Topology

from .conftest import T0, make_signal

TOPO = Topology.model_validate(
    {
        "services": {
            "web": {"depends_on": ["checkout"], "customer_facing": True},
            "checkout": {"depends_on": ["db"]},
            "orders": {"depends_on": ["db"]},
            "db": {},
            "search": {},
        }
    }
)


def at(minutes: float, **kw: object) -> object:
    return make_signal(timestamp=T0 + timedelta(minutes=minutes), **kw)


def test_deploy_shortly_before_errors_is_top_candidate() -> None:
    signals = [
        at(0, kind="deploy", service="checkout", summary="deployed v2", attrs={"version": "v2"}),
        at(3, service="web", summary="checkout page errors"),
        at(4, service="checkout", summary="5xx spike"),
        at(5, kind="deploy", service="search", summary="deployed s9"),
    ]
    result = correlate(signals, TOPO)
    top = result.candidates[0]
    assert (top.cause, top.service) == ("bad_deploy", "checkout")
    assert top.score > 0.85 and top.details["version"] == "v2"
    assert signals[3].id in result.unrelated_ids  # unrelated service set aside
    assert result.window_start == T0 + timedelta(minutes=3)


def test_deploy_after_symptoms_or_too_early_is_not_a_suspect() -> None:
    signals = [
        at(-120, kind="deploy", service="checkout", summary="old deploy"),
        at(0, service="checkout", summary="5xx spike"),
        at(2, kind="deploy", service="checkout", summary="hotfix deploy"),
    ]
    assert all(c.cause != "bad_deploy" for c in correlate(signals, TOPO).candidates)


def test_shared_dependency_and_exhaustion_patterns() -> None:
    signals = [
        at(
            0,
            kind="log",
            service="checkout",
            summary="timeout calling db",
            attrs={"level": "error"},
        ),
        at(
            1,
            kind="log",
            service="orders",
            summary="db connection timeout",
            attrs={"level": "error"},
        ),
        at(2, kind="log", service="db", summary="too many connections", attrs={"level": "error"}),
    ]
    causes = {(c.cause, c.service) for c in correlate(signals, TOPO).candidates}
    assert ("dependency_failure", "db") in causes
    assert ("resource_exhaustion", "db") in causes


def test_no_symptoms_means_no_candidates() -> None:
    result = correlate([at(0, kind="deploy", summary="deployed")], TOPO)
    assert result.candidates == [] and result.window_start is None


def test_topology_rejects_unknown_dependencies() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Topology.model_validate({"services": {"a": {"depends_on": ["ghost"]}}})
    assert TOPO.upstream_of("db") == {"checkout", "orders", "web"}
