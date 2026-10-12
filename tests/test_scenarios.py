import pytest

from orchestrator.scenarios import list_scenarios, load_spec, resolve

SCENARIOS = list_scenarios()


def test_all_nine_scenarios_present():
    assert [s.split("_")[0] for s in SCENARIOS] == [f"S{i}" for i in range(1, 10)]


@pytest.mark.parametrize("sid", SCENARIOS)
def test_resolution_is_deterministic_and_complete(sid):
    spec = load_spec(sid.split("_")[0])
    for seed in range(12):
        a, b = resolve(spec, seed), resolve(spec, seed)
        assert a.to_dict() == b.to_dict()
        assert a.change["change_type"] in {"deploy", "config", "feature_flag", "workload"}
        assert "change_causal" in a.oracle
        for fault in a.faults:
            assert fault["at_min"] + fault["duration_min"] < a.horizon_min
            assert all(not isinstance(v, dict) for v in fault.get("params", {}).values())


@pytest.mark.parametrize("sid", SCENARIOS)
def test_change_id_identifies_the_change_not_the_replicate(sid):
    spec = load_spec(sid.split("_")[0])
    n = len(spec.get("change_variants") or [{}])
    plans = [resolve(spec, seed) for seed in range(3 * n)]
    by_variant = {}
    for p in plans:
        by_variant.setdefault(p.variant_index, set()).add(p.change_id)
    assert all(len(ids) == 1 for ids in by_variant.values())  # replicates share an id
    assert len({next(iter(ids)) for ids in by_variant.values()}) == n  # variants differ


def test_oracle_is_not_part_of_the_change_block():
    plan = resolve(load_spec("S2"), 0)
    assert "oracle" not in plan.change
    assert plan.oracle["intended_effects"]
