"""Scenario manifests: loading and deterministic per-seed resolution.

A manifest (orchestrator/scenarios/<id>.yaml) declares a base change, a list of change variants,
workload variants, faults with sampled parameters, and the oracle (ground truth).
`resolve(spec, seed)` turns it into one concrete, reproducible episode plan.

change_id = scenario id + hash of the resolved change block. Replicates of the same change (other
seeds or workloads) share a change_id, so dataset splits grouped by change_id cannot leak.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

SCENARIO_DIR = Path(__file__).parent / "scenarios"
DEFAULT_WORKLOAD = {"rps": 20.0, "profile": "steady"}


@dataclass
class EpisodePlan:
    scenario: str
    name: str
    seed: int
    change_id: str
    change: dict[str, Any]
    workload: dict[str, Any]
    faults: list[dict[str, Any]]
    oracle: dict[str, Any]
    pre_min: float
    horizon_min: float
    variant_index: int
    sampled: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def list_scenarios() -> list[str]:
    return sorted(p.stem for p in SCENARIO_DIR.glob("*.yaml"))


def load_spec(scenario: str | Path) -> dict[str, Any]:
    path = Path(scenario)
    if not path.suffix:
        matches = sorted(SCENARIO_DIR.glob(f"{scenario}*.yaml"))
        if len(matches) != 1:
            raise FileNotFoundError(f"scenario {scenario!r} matched {len(matches)} manifests")
        path = matches[0]
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def sample(value: Any, rng: random.Random) -> Any:
    """Resolve {uniform: [a, b]}, {randint: [a, b]}, {choice: [...]} leaves; recurse otherwise."""
    if isinstance(value, dict):
        if set(value) == {"uniform"}:
            lo, hi = value["uniform"]
            return round(rng.uniform(lo, hi), 3)
        if set(value) == {"randint"}:
            return rng.randint(*value["randint"])
        if set(value) == {"choice"}:
            return rng.choice(value["choice"])
        return {k: sample(v, rng) for k, v in value.items()}
    if isinstance(value, list):
        return [sample(v, rng) for v in value]
    return value


def change_hash(change: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(change, sort_keys=True).encode()).hexdigest()[:8]


def resolve(spec: dict[str, Any], seed: int) -> EpisodePlan:
    rng = random.Random(f"{spec['id']}:{seed}")
    variants = spec.get("change_variants") or [{}]
    index = seed % len(variants)
    variant = copy.deepcopy(variants[index])
    oracle = deep_merge(spec.get("oracle", {}), variant.pop("oracle", {}))
    change = sample(deep_merge(spec["change"], variant), random.Random(f"{spec['id']}:v{index}"))

    workload = deep_merge(DEFAULT_WORKLOAD, spec.get("workload", {}))
    sampled = {}
    for key, choices in (spec.get("workload_variants") or {}).items():
        workload[key] = sampled[f"workload.{key}"] = rng.choice(choices)

    faults_spec = spec.get("faults", [])
    if spec.get("fault_variants"):
        choice = rng.randrange(len(spec["fault_variants"]))
        faults_spec = spec["fault_variants"][choice]
        sampled["fault_variant"] = choice
    faults = sample(faults_spec, rng)
    return EpisodePlan(
        scenario=spec["id"],
        name=spec["name"],
        seed=seed,
        change_id=f"{spec['id']}-{change_hash(change)}",
        change=change,
        workload=workload,
        faults=faults,
        oracle=oracle,
        pre_min=float(spec.get("pre_min", 30)),
        horizon_min=float(spec.get("horizon_min", 30)),
        variant_index=index,
        sampled=sampled,
    )
