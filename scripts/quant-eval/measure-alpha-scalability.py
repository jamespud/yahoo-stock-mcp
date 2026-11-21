"""Measure Alpha stock publication in a fresh registry and development window."""

import argparse
import json
import os
from pathlib import Path
import resource
import sqlite3
import tempfile
from time import monotonic

from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.calendar import generate_calendar
from stock_quant.jobs import JobStore, execute_job

START, END = "2002-01-01", "2012-01-01"


def fixture(path, count):
    calendar = generate_calendar(calendar_name="XNYS", exchange="XNYS", start_date=START, end_date=END)
    dates = [str(s.date) for s in calendar.sessions if str(s.date) < END]
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE instruments(id INTEGER PRIMARY KEY,symbol,exchange,currency);
            CREATE TABLE daily_bars(instrument_id,trade_date,open,high,low,close,volume,source,
                                    PRIMARY KEY(instrument_id,trade_date));
        """)
        db.executemany("INSERT INTO instruments VALUES (?,?,'NYSE','USD')",
                       ((i, f"S{i:03d}") for i in range(count)))
        db.executemany("INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,'yahoo')", (
            (i, day, price, price + 1, price - 1, price + .25, 1000 + j)
            for i in range(count) for j, day in enumerate(dates) for price in [50 + i + j / 100]
        ))


def json_bytes(value, *, compact=False):
    return len(json.dumps(value, allow_nan=False, **(
        {"separators": (",", ":"), "ensure_ascii": False} if compact else {}
    )).encode())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("count", type=int, choices=(50, 60, 80, 100))
    parser.add_argument("--db", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="alpha-scale-") as directory:
        root = Path(directory)
        source = args.db or root / "synthetic.db"
        if args.db is None:
            fixture(source, args.count)
        with sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True) as db:
            symbols = [row[0] for row in db.execute("""
                SELECT i.symbol FROM instruments i JOIN daily_bars b ON b.instrument_id=i.id
                WHERE b.trade_date>=? AND b.trade_date<?
                GROUP BY i.id ORDER BY count(*) DESC,i.symbol LIMIT ?
            """, (START, END, args.count))]
        if len(symbols) != args.count:
            parser.error(f"Only {len(symbols)} instruments in the development window")
        os.environ["STOCK_MCP_DB_PATH"] = str(source.resolve())
        metrics = {"source": "local_yahoo" if args.db else "synthetic", "window": [START, END], "symbols": symbols}
        with JobStore(str(root / "quant.db")) as store:
            begin_publish = store.begin_publish

            def measured_publish(claim, result):
                metrics["job_result_before_serialization_bytes"] = json_bytes(result)
                metrics["result_list_sections"] = {
                    name: {"max_row_bytes": max(map(json_bytes, rows), default=0),
                           "uncompressed_json_row_bytes": sum(json_bytes(row, compact=True) for row in rows)}
                    for name, rows in result.items() if isinstance(rows, list)
                }
                start = monotonic()
                try:
                    return begin_publish(claim, result)
                finally:
                    metrics["result_serialization_seconds"] = monotonic() - start

            store.begin_publish = measured_publish
            job = store.submit_alpha({"kind": "prepare_dataset", "symbols": symbols,
                                      "start": START + "T00:00:00Z", "end": END + "T00:00:00Z"})
            started = monotonic()
            execute_job(store, store.claim())
            metrics["execution_seconds"] = monotonic() - started
            metrics["job"] = store.get_job(job["job_id"])
            payload = store.connection.execute("SELECT result FROM jobs WHERE id=?", (job["job_id"],)).fetchone()[0]
            metrics["job_persisted_metadata_bytes"] = len(payload.encode()) if payload else None
            manifests = list((root / "quant.alpha" / "datasets").glob("*/manifest.json"))
            if manifests:
                path, = manifests
                manifest = json.loads(path.read_bytes())
                metrics["dataset_id"] = manifest["dataset_id"]
                metrics["manifest_bytes"] = path.stat().st_size
                try:
                    DatasetStore(root / "quant.alpha").load_manifest(manifest["dataset_id"])
                    metrics["registered"] = True
                except KeyError:
                    metrics["registered"] = False
                metrics["partition_bytes"] = {str(p.relative_to(path.parent)): p.stat().st_size
                                              for p in sorted(path.parent.rglob("*.parquet"))}
                metrics["total_parquet_bytes"] = sum(metrics["partition_bytes"].values())
                metrics["report_bytes"] = json_bytes(manifest["data_quality"])
                metrics["mapping_bytes"] = json_bytes(manifest["data_quality"].get("exchange_mappings"))
                metrics["partition_manifest_bytes"] = json_bytes(manifest["partition_manifest"])
            if payload:
                metrics["dataset_bridge_json_bytes"] = json_bytes({"result": store.get_result(job["job_id"], "dataset")}) + 1
            metrics["peak_rss_ru_maxrss"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
