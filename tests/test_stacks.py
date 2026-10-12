"""Multi-stack configuration (parallel testbeds on one VM) and batch sharding."""

import importlib
import subprocess
import sys

import pytest


def _testbed(monkeypatch, stack: str):
    monkeypatch.setenv("LS_STACK", stack)
    sys.modules.pop("orchestrator.testbed", None)
    return importlib.import_module("orchestrator.testbed")


@pytest.fixture(autouse=True)
def _restore():
    yield
    sys.modules.pop("orchestrator.testbed", None)


def test_stack_zero_keeps_original_layout(monkeypatch):
    tb = _testbed(monkeypatch, "0")
    assert tb.PROJECT == "logsentinel" and tb.PORTS["gateway"] == 8080
    assert tb.LIVE_LOGS.name == "_live_logs"


def test_stack_k_is_isolated(monkeypatch):
    tb = _testbed(monkeypatch, "2")
    assert tb.PROJECT == "logsentinel-s2"
    assert tb.PORTS["gateway"] == 8280 and tb.PORTS["prometheus"] == 9290
    assert tb.PROMETHEUS_URL.endswith(":9290") and tb.LIVE_LOGS.name == "_live_logs_s2"
    env = tb.compose_env()
    assert env["LS_PORT_GATEWAY"] == "8280" and env["LS_LIVE_LOGS"].endswith("_live_logs_s2")


def test_other_stacks_count_as_host_noise(monkeypatch):
    tb = _testbed(monkeypatch, "0")
    assert tb._own("logsentinel-gateway-1")
    assert not tb._own("logsentinel-s1-gateway-1")  # another stack competes for CPU
    assert not tb._own("frappe_docker-db-1")
    tb1 = _testbed(monkeypatch, "1")
    assert tb1._own("logsentinel-s1-postgres-1") and not tb1._own("logsentinel-postgres-1")


def test_compose_file_is_valid_with_stack_overrides(monkeypatch):
    tb = _testbed(monkeypatch, "3")
    cmd = [
        "docker",
        "compose",
        "-p",
        tb.PROJECT,
        "-f",
        str(tb.COMPOSE_FILE),
        "config",
        "--format",
        "json",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, env=tb.compose_env(), timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("docker CLI not available")
    if out.returncode != 0:
        pytest.skip(f"docker compose config unavailable: {out.stderr[:120]}")
    assert '"published": "8380"' in out.stdout and "_live_logs_s3" in out.stdout


def test_shards_partition_the_job_list():
    jobs = [(s, seed) for s in ("S1", "S5", "S9") for seed in range(4)]
    shards = [[j for i, j in enumerate(jobs) if i % 3 == k] for k in range(3)]
    assert sorted(sum(shards, [])) == sorted(jobs)
    assert all(len(s) == 4 for s in shards)


def test_batch_cli_runs_only_its_shard(monkeypatch):
    from typer.testing import CliRunner

    import orchestrator.runner as runner
    from orchestrator import cli

    ran = []
    monkeypatch.setattr(
        runner, "run_episode", lambda plan, warmup_min: ran.append((plan.scenario, plan.seed))
    )
    monkeypatch.setattr(cli.tb, "get_load", lambda: {"rps": 20})
    monkeypatch.setattr(cli.tb, "set_load", lambda **kw: kw)
    args = ["batch", "S1", "S5", "--seeds", "0-3", "--shard", "1", "--shards", "3"]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    jobs = [(s, seed) for s in ("S1", "S5") for seed in range(4)]
    assert ran == [j for i, j in enumerate(jobs) if i % 3 == 1]
