import subprocess
import sys
from pathlib import Path


def test_base_runtime_import_does_not_require_alpha_or_options_dependencies():
    code = r'''
import builtins
blocked = {"lightgbm", "sklearn", "scipy", "QuantLib", "httpx", "polars", "duckdb"}
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split(".")[0] in blocked:
        raise ModuleNotFoundError(name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import stock_quant.__main__
print("ok")
'''
    env = {"PYTHONPATH": str(Path(__file__).parents[2] / "src")}
    result = subprocess.run([sys.executable, "-c", code], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
