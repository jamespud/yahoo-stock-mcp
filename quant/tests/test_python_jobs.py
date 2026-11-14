"""Immutable Python plan references and fenced jobs use the same control plane."""
import os

import pytest

from stock_quant.jobs import JobStore, execute_job
from test_runtime import fixture


def definition():
    return {"mode": "python", "name": "hold-python", "source": '''
class Strategy:
    def on_bar(self, ctx, bar):
        if ctx.position(bar.symbol) == 0:
            return [{"symbol": bar.symbol, "side": "buy", "quantity": 2}]
        return []
'''}


def test_python_register_is_immutable_syntax_only_without_enabled_execution(tmp_path, monkeypatch):
    monkeypatch.delenv("QUANT_SANDBOX_IMAGE", raising=False)
    with JobStore(tmp_path / "registry.db") as store:
        a = store.register(definition())
        assert store.register(definition()) == a
        assert store.get_strategy(a["strategy_id"])["definition"]["mode"] == "python"
        with pytest.raises(ValueError, match="SANDBOX_UNAVAILABLE"):
            store.compile(a["strategy_id"])


def test_python_plan_and_job_survive_registry_restart_and_use_pinned_image(tmp_path, monkeypatch):
    image = os.environ.get("QUANT_SANDBOX_IMAGE")
    if not image:
        pytest.skip("actual Python strategy jobs require QUANT_SANDBOX_IMAGE")
    path = tmp_path / "registry.db"
    with JobStore(path) as store:
        strategy_id = store.register(definition())["strategy_id"]
        plan = store.compile(strategy_id)
        assert plan["mode"] == "python"
        assert plan["sandbox_image"] == image
        data = fixture()
        snap = store.import_snapshot([b.model_dump(mode="json") for b in data.bars], data.calendar_version)
        job = store.submit({"plan_id": plan["plan_id"], "symbol": "AAPL", "timeframe": "1d", "data_snapshot_id": snap["data_snapshot_id"]})
    with JobStore(path) as store:
        assert store.compile(strategy_id)["plan_id"] == plan["plan_id"]
        claim = store.claim()
        assert execute_job(store, claim)
        assert store.get_job(job["job_id"])["status"] == "completed"
        assert store.get_result(job["job_id"], "final_account")["positions"]["AAPL"]["quantity"] == 2.
        repro = store.get_result(job["job_id"], "reproducibility")
        assert repro["sandbox_image"] == image
        assert repro["plan_hashes"] == {"AAPL": plan["plan_id"]}
        monkeypatch.delenv("QUANT_SANDBOX_IMAGE")
        with pytest.raises(ValueError, match="SANDBOX_UNAVAILABLE"):
            store.load_plan(plan["plan_id"])


def test_python_plan_pins_runtime_and_allows_one_artifact_with_per_symbol_bindings(tmp_path, monkeypatch):
    image = os.environ.get("QUANT_SANDBOX_IMAGE")
    if not image:
        pytest.skip("sandbox operator image required")
    with JobStore(tmp_path / "registry.db") as store:
        strategy_id = store.register(definition())["strategy_id"]
        monkeypatch.setenv("QUANT_SANDBOX_RUNTIME", "runsc")
        pinned = store.compile(strategy_id)
        monkeypatch.setenv("QUANT_SANDBOX_RUNTIME", "runc")
        with pytest.raises(ValueError, match="integrity|policy"):
            store.load_plan(pinned["plan_id"])
        a = store.compile(strategy_id, symbol="AAPL")
        b = store.compile(strategy_id, symbol="MSFT")
        assert a["plan_id"] != b["plan_id"]
        data = fixture()
        snap = store.import_snapshot([row.model_dump(mode="json") for row in [*data.bars, *[bar.model_copy(update={"symbol": "MSFT"}) for bar in data.bars]]], "bound-python")
        job = store.submit({"portfolio": {"AAPL": a["plan_id"], "MSFT": b["plan_id"]}, "data_snapshot_id": snap["data_snapshot_id"]})
        assert execute_job(store, store.claim())
        assert store.get_job(job["job_id"])["status"] == "completed"
        assert store.get_result(job["job_id"], "final_account")["positions"]["MSFT"]["quantity"] == 2.


def test_python_job_seed_controls_module_constructor_and_lifecycle_randomness(tmp_path):
    import random
    if not os.environ.get("QUANT_SANDBOX_IMAGE"):
        pytest.skip("actual seeded Python jobs require QUANT_SANDBOX_IMAGE")
    artifact = {"mode": "python", "name": "seeded", "source": '''
import random
module_draw = random.random()
class Strategy:
    def __init__(self):
        self.base = module_draw + random.random()
    def initialize(self, ctx):
        self.seed = ctx.random_seed
        self.local_draw = random.Random(ctx.random_seed).random()
    def on_bar(self, ctx, bar):
        assert ctx.random_seed == self.seed
        if ctx.position(bar.symbol) == 0:
            return [{"symbol": bar.symbol, "side": "buy", "quantity": self.base + self.local_draw + random.random()}]
        return []
    def on_fill(self, ctx, fill):
        assert ctx.random_seed == self.seed
'''}
    with JobStore(tmp_path / "seeded.db") as store:
        sid = store.register(artifact)["strategy_id"]
        pid = store.compile(sid)["plan_id"]
        data = fixture()
        snapshot = store.import_snapshot([b.model_dump(mode="json") for b in data.bars], data.calendar_version)
        runs = []
        for seed in (27, 27, 81):
            job = store.submit({"plan_id": pid, "symbol": "AAPL", "timeframe": "1d", "data_snapshot_id": snapshot["data_snapshot_id"], "random_seed": seed})
            assert execute_job(store, store.claim())
            assert store.get_job(job["job_id"])["status"] == "completed", store.get_job(job["job_id"])
            runs.append({section: store.get_result(job["job_id"], section) for section in ("orders", "fills", "cash_ledger", "equity_curve", "final_account", "reproducibility")})
            for section in ("orders", "fills", "cash_ledger", "equity_curve"):
                # Domain content and its hash repeat; job resource addresses differ.
                runs[-1][section].pop("resource_uri")
            rng = random.Random(seed)
            expected = rng.random() + rng.random() + random.Random(seed).random() + rng.random()
            assert runs[-1]["fills"]["rows"][0]["quantity"] == expected
            assert runs[-1]["reproducibility"]["random_seed"] == seed
        assert runs[0] == runs[1]
        assert runs[0]["fills"] != runs[2]["fills"]
