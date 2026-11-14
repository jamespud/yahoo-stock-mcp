"""Content identities for reproducible development and installed runtime builds."""
from functools import lru_cache
import hashlib
from importlib.metadata import version
from pathlib import Path
import platform
import sys


@lru_cache(maxsize=None)
def source_identity(*modules: str) -> str:
    digest = hashlib.sha256()
    for module in sorted(modules):
        digest.update(module.encode())
        digest.update(b"\0")
        digest.update(Path(__file__).with_name(module + ".py").read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def environment() -> dict:
    return {"python": sys.version, "platform": platform.platform(), "pydantic": version("pydantic"), "numpy": version("numpy"), "pyarrow": version("pyarrow"), "exchange_calendars": version("exchange-calendars"), "pandas": version("pandas"), "tzdata": version("tzdata"),
            "runtime_source_hash": source_identity("calendar", "quality", "analytics", "artifacts", "runtime", "vector", "numerics", "data", "contracts", "compiler", "jobs", "provenance", "portfolio", "actions", "research", "registry", "python_plans", "strategy_runtime", "sandbox", "sandbox_guest", "sandbox_watchdog")}
