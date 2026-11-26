"""Detached trusted job consumer. No client pipe is retained by this process."""
import argparse
import os
import signal
import subprocess
import sys

from .alpha.runtime_budget import hard_attempt_seconds
from .jobs import JobStore, execute_job


def wake_worker(path: str):
    # Every consumer exits after the durable queue drains. A SQLite transaction
    # enforces the shared concurrency bound even across many MCP processes.
    process = subprocess.Popen([sys.executable, "-m", "stock_quant.worker", path],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True,
                               close_fds=True)
    return process.pid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("registry_path")
    args = parser.parse_args()
    with JobStore(args.registry_path) as store:
        while (claim := store.claim()) is not None:
            # Hard wall-clock guard also bounds a stuck evaluator between checks.
            # A killed worker leaves its lease to expire for fenced retry.
            if hasattr(signal, "alarm"):
                signal.alarm(hard_attempt_seconds(claim["request"]))
            execute_job(store, claim)
            if hasattr(signal, "alarm"):
                signal.alarm(0)


if __name__ == "__main__":
    main()
