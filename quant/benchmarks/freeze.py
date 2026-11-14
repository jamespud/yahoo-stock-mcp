"""Check or deliberately regenerate independent versioned fixture/oracle pins."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from reliability.benchmarks import CATALOG, definitions
from reliability.market import MANIFEST, canonical_hash, daily_payload, payload, snapshot
from reliability.oracles import account
from reliability.required import manifest as required_manifest


def artifacts():
    raw, daily = payload(), daily_payload()
    market = snapshot(raw)
    manifest = {k: v for k, v in raw.items() if k != "bars"}
    manifest.update(payload_hash=canonical_hash(raw), bars_hash=canonical_hash(raw["bars"]), snapshot_id=market.id, bar_count=len(raw["bars"]))
    config = {"initial_cash": 100000., "quantity": 3., "commission_bps": 7., "slippage_bps": 11.}
    cases = definitions()
    for case in cases:
        case.update(strategy_hash=canonical_hash(case["strategy"]), expected=account(case["strategy"], market.bars, **config))
    daily_manifest = {"version": daily["version"], "seed": daily["seed"], "bar_count": len(daily["bars"]), "session_count": len(daily["calendars"][0]["sessions"]), "regimes": daily["regimes"], "payload_hash": canonical_hash(daily), "snapshot_id": snapshot(daily).id}
    return {CATALOG.with_name("manifest_v1.json"): required_manifest(), MANIFEST: manifest, MANIFEST.with_name("phase2_daily_v1.json"): daily_manifest,
            CATALOG: {"version": "phase2-strategies-v1", "fixture_version": raw["version"], "fixture_hash": manifest["payload_hash"], "snapshot_id": market.id, "execution_config": config, "cases": cases}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Deliberately replace checked-in pins after reviewing fixture/oracle changes")
    args = parser.parse_args()
    for path, expected in artifacts().items():
        if args.write:
            path.write_text(json.dumps(expected, indent=2, allow_nan=False) + "\n")
        elif json.loads(path.read_text()) != expected:
            raise SystemExit(f"versioned reference mismatch: {path}")
        print(f"{'WROTE' if args.write else 'PASS'} {path.name}")
