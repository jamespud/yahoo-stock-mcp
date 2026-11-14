"""Production acceptance against independent references and adverse mutations."""
import copy
from datetime import timedelta
import json
from math import isclose
from random import Random

from hypothesis import given, settings, strategies as st
import pytest

from stock_quant.compiler import compile_strategy
from stock_quant.data import Snapshot
from stock_quant.runtime import Evaluator, ExecutionConfig, run_backtest
from stock_quant.vector import VectorEvaluator
from reliability.market import MANIFEST, SYMBOLS, canonical_hash, payload, snapshot
from reliability.benchmarks import catalog, definitions
from reliability.oracles import account, indicator, visible


@pytest.fixture(scope="module")
def market():
    return snapshot()


def assert_account(result, expected):
    got = result["final_account"]
    for key in ("cash", "equity"):
        assert got[key] == pytest.approx(expected[key], abs=1e-7)
    for key in ("quantity", "mark", "basis", "realized_pnl"):
        assert got["positions"]["AAPL"][key] == pytest.approx(expected[key], abs=1e-7)
    assert result["summary"]["max_drawdown"] == pytest.approx(expected["max_drawdown"], abs=1e-10)
    assert result["attribution"]["unrealized_pnl"] == pytest.approx(expected["unrealized_pnl"], abs=1e-7)
    assert len(result["equity_curve"]) == len(expected["equity_curve"])
    for actual, want in zip(result["equity_curve"], expected["equity_curve"]):
        assert actual["time"] == want["time"]
        for key in ("cash", "equity"):
            assert actual[key] == pytest.approx(want[key], abs=1e-7)
        position = actual["positions"]["AAPL"]
        for key in ("quantity", "mark", "basis", "realized_pnl"):
            assert position[key] == pytest.approx(want[key], abs=1e-7)
        assert position["quantity"] * position["mark"] - position["basis"] == pytest.approx(want["unrealized_pnl"], abs=1e-7)
    assert len(result["orders"]) == len(expected["orders"])
    for actual, want in zip(result["orders"], expected["orders"]):
        for key, value in want.items():
            assert actual[key] == value
    assert len(result["fills"]) == len(expected["fills"])
    for actual, want in zip(result["fills"], expected["fills"]):
        for key in ("time", "side"):
            assert actual[key] == want[key]
        for key in ("quantity", "price", "commission"):
            assert actual[key] == pytest.approx(want[key], abs=1e-9)


@pytest.mark.parametrize("case", catalog()["cases"], ids=lambda c: "golden_oracle_" + c["id"])
def test_phase2_strategy_golden_oracle(case, market):
    cat = catalog()
    assert canonical_hash(case["strategy"]) == case["strategy_hash"]
    expected = account(case["strategy"], market.bars, **cat["execution_config"])
    # Pins the independent reference too: accidental oracle edits do not bless drift.
    assert expected == case["expected"]
    plan = compile_strategy(case["strategy"], symbol="AAPL")
    result = run_backtest(plan, market, symbol="AAPL", timeframe=plan.decision_timeframe,
                          config=ExecutionConfig(**cat["execution_config"]))
    assert_account(result, expected)
    assert result["summary"]["total_return"] == pytest.approx(expected["equity"] / cat["execution_config"]["initial_cash"] - 1)
    orders = {o["id"]: o for o in result["orders"]}
    for fill in result["fills"]:
        assert fill["time"] > orders[fill["order_id"]]["submitted_at"]


def test_calendar_fixture_hash_regimes_and_alignment_boundaries(market):
    raw = payload()
    manifest = json.loads(MANIFEST.read_text())
    assert canonical_hash(raw) == manifest["payload_hash"] == catalog()["fixture_hash"]
    assert canonical_hash(raw["bars"]) == manifest["bars_hash"]
    assert market.id == manifest["snapshot_id"] == catalog()["snapshot_id"]
    assert len(market.bars) == manifest["bar_count"] == 1176
    assert catalog()["version"] == "phase2-strategies-v1"
    assert [c["strategy"] for c in catalog()["cases"]] == [c["strategy"] for c in definitions()]
    assert len({r["name"] for r in raw["regimes"]}) == 4
    assert {b.symbol for b in market.bars} == set(SYMBOLS)
    assert {b.timeframe for b in market.bars} == {"1d", "1h", "15m"}
    sessions = {s["date"]: s["intervals"][0] for s in raw["calendars"][0]["sessions"]}
    assert sessions["2026-03-06"]["start"].endswith("14:30:00+00:00")
    assert sessions["2026-03-09"]["start"].endswith("13:30:00+00:00")
    assert sessions["2026-10-30"]["start"].endswith("13:30:00+00:00")
    assert sessions["2026-11-02"]["start"].endswith("14:30:00+00:00")
    assert sessions["2026-11-27"]["end"].endswith("18:00:00+00:00")
    for symbol in SYMBOLS:
        for tf in ("1d", "1h", "15m"):
            prices = [b.close for b in sorted(market.bars, key=lambda b: b.end_time) if b.symbol == symbol and b.timeframe == tf]
            assert any(a < b for a, b in zip(prices, prices[1:]))
            assert any(a > b for a, b in zip(prices, prices[1:]))
    from stock_quant.calendar import expected_intervals
    for tf, total in (("1d", 12), ("1h", 81), ("15m", 300)):
        assert len(expected_intervals(market.calendars[0], tf)) == total
    for row in raw["missing"]:
        assert not any(b.symbol == row["symbol"] and b.timeframe == row["timeframe"] and b.start_time.isoformat() == row["start_time"] for b in market.bars)
    for row in raw["delayed"]:
        b = next(b for b in market.bars if b.symbol == row["symbol"] and b.timeframe == row["timeframe"] and b.start_time.isoformat() == row["start_time"])
        assert b not in market.history(b.symbol, b.timeframe, b.available_at - timedelta(microseconds=1), 100000)
        assert b in market.history(b.symbol, b.timeframe, b.available_at, 100000)


def indicator_plan(op, tf="15m", period=4):
    return compile_strategy({"schema_version": "1.0", "name": "oracle", "decision_timeframe": tf,
                             "signals": {"score": {"kind": "call", "operator": op, "args": [{"kind": "ref", "symbol": "AAPL", "field": "close", "timeframe": tf}], "params": {"period": period}}},
                             "entry": {"kind": "binary", "op": "gt", "left": {"kind": "signal", "name": "score"}, "right": {"kind": "const", "value": 90}}, "exit": {"kind": "const", "value": False}})


@pytest.mark.parametrize("tf", ["1d", "1h", "15m"])
@pytest.mark.parametrize("op", ["sma", "ema", "rsi"])
def test_indicator_oracle_nonmonotonic_delayed_publication(market, op, tf):
    plan = indicator_plan(op, tf)
    ev = Evaluator(plan, market, symbol="AAPL")
    times = sorted({b.available_at for b in market.bars if b.symbol == "AAPL" and b.timeframe == tf})
    # Independent Decimal calculation checks warmup, nonlinear path and late history.
    for now in times[::max(1, len(times) // 24)]:
        prices = [b.close for b in visible(market.bars, "AAPL", tf, now)]
        expected = indicator(prices, op, 4)
        actual = ev.value(plan.named_nodes["score"], now)
        assert actual is None if expected is None else actual == pytest.approx(expected, abs=1e-9)


@settings(max_examples=24, deadline=None, derandomize=True)
@given(index=st.integers(5, 285), multiplier=st.floats(min_value=1.2, max_value=30, allow_nan=False, allow_infinity=False), mode=st.sampled_from(["price", "availability", "delete", "reorder", "append"]), op=st.sampled_from(["sma", "ema", "rsi"]))
def test_future_availability_mutation_property(index, multiplier, mode, op):
    base = snapshot()
    bars = list(base.bars)
    times = sorted({b.available_at for b in bars if b.symbol == "AAPL" and b.timeframe == "15m"})
    now = times[index]
    future = [b for b in bars if b.available_at > now]
    assert future
    changed = []
    for b in bars:
        if b.available_at > now:
            if mode == "delete":
                continue
            if mode == "price":
                b = b.model_copy(update={k: getattr(b, k) * multiplier for k in ("open", "high", "low", "close")})
            if mode == "availability":
                b = b.model_copy(update={"available_at": b.available_at + timedelta(days=3)})
        changed.append(b)
    if mode == "append":
        last = max((b for b in bars if b.symbol == "AAPL" and b.timeframe == "15m"), key=lambda b: b.end_time)
        changed.append(last.model_copy(update={"start_time": last.start_time + timedelta(days=1), "end_time": last.end_time + timedelta(days=1), "available_at": last.available_at + timedelta(days=1)}))
    if mode == "reorder":
        Random(index).shuffle(changed)
    mutant = Snapshot(changed, calendar_version=base.calendar_version, calendars=base.calendars)
    if mode == "reorder":
        assert mutant.id == base.id
    else:
        assert mutant.id != base.id
    plan = indicator_plan(op)
    expected = indicator([b.close for b in visible(bars, "AAPL", "15m", now)], op, 4)
    for data in (base, mutant):
        assert Evaluator(plan, data).value(plan.named_nodes["score"], now) == pytest.approx(expected, abs=1e-9)
    # Time-bound broker assertions include orders/fills/accounts, not hashes.
    stop = now + timedelta(microseconds=1)
    a = run_backtest(plan, base, symbol="AAPL", timeframe="15m", end=stop)
    # Broker already knows opens, independently of close publication. Preserve
    # all started bars when mutating the broker prefix.
    broker_rows = [b for b in bars if b.start_time <= now] + [b for b in mutant.bars if b.start_time > now]
    broker_mutant = Snapshot([b for b in broker_rows if b is not None], calendar_version=base.calendar_version, calendars=base.calendars)
    b = run_backtest(plan, broker_mutant, symbol="AAPL", timeframe="15m", end=stop)
    for section in ("orders", "fills", "final_account", "cash_ledger"):
        assert a[section] == b[section]


def test_future_leak_mutant_is_killed_by_indicator_oracle(market, monkeypatch):
    plan = indicator_plan("sma")
    now = sorted(b.available_at for b in market.bars if b.symbol == "AAPL" and b.timeframe == "15m")[8]
    expected = indicator([b.close for b in visible(market.bars, "AAPL", "15m", now)], "sma", 4)
    original = Snapshot.history
    def leaking(self, symbol, tf, at, bars):
        return original(self, symbol, tf, max(b.available_at for b in self.bars), bars)
    monkeypatch.setattr(Snapshot, "history", leaking)
    actual = Evaluator(plan, market).value(plan.named_nodes["score"], now)
    assert not isclose(actual, expected, abs_tol=1e-6), "oracle failed to kill future-history leak"


@pytest.mark.parametrize("kind", ["nan", "negative", "ohlc", "publication", "duplicate", "overlap", "mixed_version"])
def test_corrupt_fixture_rejected_at_production_boundary(market, kind):
    rows = list(market.bars)
    b = rows[0]
    changes = {"nan": {"close": float("nan")}, "negative": {"volume": -1.}, "ohlc": {"high": b.low / 2},
               "publication": {"available_at": b.start_time}, "mixed_version": {"dataset_version": "corrupt"}}
    if kind == "duplicate": rows.append(b)
    elif kind == "overlap": rows.append(b.model_copy(update={"start_time": b.start_time + timedelta(seconds=1)}))
    else: rows[0] = b.model_copy(update=changes[kind])
    with pytest.raises(ValueError):
        Snapshot(rows, calendar_version=market.calendar_version)


def test_corrupt_artifact_payload_detected(tmp_path):
    from stock_quant.artifacts import ArtifactStore
    store = ArtifactStore(tmp_path)
    ref = store.write_rows(iter([{"cash": 1000., "quantity": 3.}]))
    path = store.resolve(ref)
    content = bytearray(path.read_bytes())
    content[len(content) // 2] ^= 1
    path.write_bytes(content)
    with pytest.raises(ValueError, match="integrity"):
        store.read_rows(ref, 0, 1)


@pytest.mark.parametrize("commission,slippage", [(0., 0.), (7., 11.), (40., 60.)], ids=["zero_cost", "commission_slippage", "stressed_cost"])
def test_cost_commission_slippage_account_oracle(market, commission, slippage):
    case = catalog()["cases"][1]
    config = {**catalog()["execution_config"], "commission_bps": commission, "slippage_bps": slippage}
    expected = account(case["strategy"], market.bars, **config)
    plan = compile_strategy(case["strategy"], symbol="AAPL")
    actual = run_backtest(plan, market, symbol="AAPL", timeframe="1d", config=ExecutionConfig(**config))
    assert_account(actual, expected)
    assert sum(f["commission"] for f in actual["fills"]) == pytest.approx(sum(f["quantity"] * f["price"] * commission / 10000 for f in expected["fills"]))
    free = account(case["strategy"], market.bars, **{**config, "commission_bps": 0., "slippage_bps": 0.})
    assert expected["equity"] <= free["equity"]


@pytest.mark.parametrize("op", ["sma", "ema", "rsi"])
def test_vector_indicator_oracle_and_future_query_isolation(market, op):
    plan = indicator_plan(op, "1h")
    vector = VectorEvaluator(plan, market, symbol="AAPL")
    times = sorted({b.available_at for b in market.bars if b.symbol == "AAPL" and b.timeframe == "1h"})
    # Query later first to catch caches that leak a future state into past calls.
    for now in (times[-1], times[8], times[3], times[40], times[8]):
        expected = indicator([b.close for b in visible(market.bars, "AAPL", "1h", now)], op, 4)
        actual = vector.value(plan.named_nodes["score"], now)
        assert actual is None if expected is None else actual == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("index", [5, 6], ids=["daily_hourly_alignment_future_mutation", "hourly_15m_alignment_future_mutation"])
def test_alignment_future_available_price_mutation_oracle(market, index):
    from reliability.oracles import value
    spec = catalog()["cases"][index]["strategy"]
    plan = compile_strategy(spec, symbol="AAPL")
    times = sorted({b.available_at for b in market.bars if b.symbol == "AAPL" and b.timeframe == spec["decision_timeframe"]})
    now = times[len(times) // 2]
    rows = [b.model_copy(update={k: getattr(b, k) * 20 for k in ("open", "high", "low", "close")}) if b.available_at > now else b for b in market.bars]
    mutant = Snapshot(rows, calendar_version=market.calendar_version, calendars=market.calendars)
    expected = value(spec["entry"], market.bars, now, "AAPL", {})
    assert expected is not None
    for data in (market, mutant):
        ev = Evaluator(plan, data, symbol="AAPL")
        ev.value(plan.entry.id, times[-1])
        assert ev.value(plan.entry.id, now) is expected


def test_account_oracle_kills_fee_and_fill_timing_corruption(market):
    case = catalog()["cases"][1]
    plan = compile_strategy(case["strategy"], symbol="AAPL")
    config = ExecutionConfig(**catalog()["execution_config"])
    valid = run_backtest(plan, market, symbol="AAPL", timeframe="1d", config=config)
    for mutation in ("commission", "same_bar_fill", "drawdown", "unrealized", "curve"):
        altered = copy.deepcopy(valid)
        if mutation == "commission": altered["fills"][0]["commission"] = 0.
        elif mutation == "same_bar_fill": altered["fills"][0]["time"] = altered["orders"][0]["submitted_at"]
        elif mutation == "drawdown": altered["summary"]["max_drawdown"] = 0.
        elif mutation == "unrealized": altered["attribution"]["unrealized_pnl"] += 1.
        else: altered["equity_curve"][3]["equity"] += 1.
        with pytest.raises(AssertionError):
            assert_account(altered, case["expected"])


def test_seed_fixture_reproducibility_and_valid_raw_ohlcv():
    from datetime import datetime
    first, repeat, changed = payload(80421), payload(80421), payload(80422)
    assert canonical_hash(first) == canonical_hash(repeat)
    assert canonical_hash(first) != canonical_hash(changed)
    for row in first['bars']:
        assert row['high'] >= max(row['open'], row['close'])
        assert row['low'] <= min(row['open'], row['close'])
        assert row['volume'] >= 0
        start, end, available = [datetime.fromisoformat(row[key]) for key in ['start_time', 'end_time', 'available_at']]
        assert start.tzinfo and end.tzinfo and available.tzinfo
        assert start < end <= available
        assert row['adjustment'] == 'raw' and row['source'] == 'artificial_seeded'
