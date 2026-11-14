"""Actual-container acceptance. Set QUANT_SANDBOX_IMAGE to a trusted image ID."""
from datetime import timedelta
import os
import subprocess
import time

import pytest
from pydantic import ValidationError

from test_alignment import fixture
from test_data import T


@pytest.fixture
def image():
    value = os.environ.get("QUANT_SANDBOX_IMAGE")
    if not value:
        pytest.skip("actual Docker sandbox tests require a trusted QUANT_SANDBOX_IMAGE ID")
    return value


def artifact(source):
    from stock_quant.sandbox import PythonArtifact
    return PythonArtifact(name="test", source=source, data_timeframes=["1d", "15m"])


def test_artifact_syntax_validation_never_executes_host_code(tmp_path):
    marker = tmp_path / "executed"
    from stock_quant.sandbox import PythonArtifact, validate_python
    result = validate_python({"name": "syntax-only", "source": f'open({str(marker)!r}, "w").write("unsafe")\nclass Strategy: pass'})
    assert result["valid"]
    assert not marker.exists()
    assert validate_python({"name": "invalid", "source": "class bad syntax"})["errors"][0]["code"] == "SCHEMA_INVALID"
    with pytest.raises(ValidationError):
        PythonArtifact(name="oversize", source="x" * 65537)


def test_missing_immutable_image_fails_closed(monkeypatch):
    from stock_quant.sandbox import SandboxSession
    monkeypatch.delenv("QUANT_SANDBOX_IMAGE", raising=False)
    with pytest.raises(ValueError, match="SANDBOX_UNAVAILABLE"):
        SandboxSession(artifact("raise RuntimeError('must not execute')"), symbols=["AAPL"])


def test_lifecycle_state_pit_history_and_account_views(image):
    from stock_quant.sandbox import SandboxSession
    source = '''
class Strategy:
    def initialize(self, ctx):
        self.count = 0
        assert ctx.position("AAPL") == 0
        assert ctx.equity() == 1000
    def on_bar(self, ctx, bar):
        self.count += 1
        history = ctx.history("AAPL", "1d", 10)
        assert len(history) == 1
        assert history[0]["close"] == 10
        assert ctx.now.isoformat() == bar["available_at"]
        return [{"symbol": "AAPL", "side": "buy", "quantity": self.count}]
    def on_fill(self, ctx, fill):
        assert fill["side"] == "buy"
        self.count = 10
'''
    data = fixture()
    bar = next(b for b in data.bars if b.timeframe == "15m")
    with SandboxSession(artifact(source), symbols=["AAPL"], image=image) as session:
        assert session.callback("initialize", now=T, snapshot=data, positions={"AAPL": 0.}, equity=1000.) == []
        first = session.callback("on_bar", now=bar.available_at, bar=bar, snapshot=data, positions={"AAPL": 0.}, equity=1000.)
        assert first == [{"symbol": "AAPL", "side": "buy", "quantity": 1.}]
        session.callback("on_fill", now=bar.available_at, fill={"side": "buy", "symbol": "AAPL", "order_id": "order_1", "time": bar.available_at.isoformat(), "quantity": 1., "price": 10., "commission": 0.}, snapshot=data, positions={"AAPL": 1.}, equity=1000.)
        assert session.callback("on_bar", now=bar.available_at, bar=bar, snapshot=data, positions={"AAPL": 1.}, equity=1000.)[0]["quantity"] == 11.


def test_host_files_network_root_writes_privileges_and_syscalls_are_denied(image, tmp_path, monkeypatch):
    from stock_quant.sandbox import SandboxSession
    secret = tmp_path / "host-secret"
    secret.write_text("host-data-must-stay-out")
    monkeypatch.setenv("QUANT_HOST_TEST_SECRET", "must-not-cross-environment")
    source = f'''
import os, socket, ctypes
class Strategy:
    def initialize(self, ctx):
        assert os.getuid() == 65534
        assert os.environ.get("QUANT_HOST_TEST_SECRET") is None
        try:
            open({str(secret)!r}).read()
        except OSError:
            pass
        else:
            raise AssertionError("host file readable")
        try:
            open("/root-write", "w").write("x")
        except OSError:
            pass
        else:
            raise AssertionError("root writable")
        try:
            socket.create_connection(("1.1.1.1", 443), timeout=.2)
        except OSError:
            pass
        else:
            raise AssertionError("network allowed")
        assert ctypes.CDLL(None).unshare(0x10000000) == -1
        status = open("/proc/self/status").read()
        assert "NoNewPrivs:\\t1" in status
        assert "Seccomp:\\t2" in status
        assert "CapEff:\\t0000000000000000" in status
        open("/tmp/allowed", "w").write("scratch")
'''
    with SandboxSession(artifact(source), symbols=["AAPL"], image=image) as session:
        assert session.callback("initialize", now=T, snapshot=fixture(), positions={}, equity=1000.) == []
    assert secret.read_text() == "host-data-must-stay-out"


def test_history_cannot_request_unauthorized_symbol_or_future_clock(image):
    from stock_quant.sandbox import SandboxSession
    source = '''
class Strategy:
    def initialize(self, ctx):
        ctx._rpc({"type": "history", "symbol": "AAPL", "timeframe": "1d", "bars": 1, "as_of": "2099-01-01T00:00:00Z"})
'''
    with SandboxSession(artifact(source), symbols=["AAPL"], image=image) as session:
        with pytest.raises(ValueError, match="SANDBOX_POLICY_VIOLATION"):
            session.callback("initialize", now=T, snapshot=fixture(), positions={}, equity=1000.)
    source = source.replace('"AAPL", "timeframe"', '"MSFT", "timeframe"').replace(', "as_of": "2099-01-01T00:00:00Z"', '')
    with SandboxSession(artifact(source), symbols=["AAPL"], image=image) as session:
        with pytest.raises(ValueError, match="SANDBOX_POLICY_VIOLATION"):
            session.callback("initialize", now=T, snapshot=fixture(), positions={}, equity=1000.)


@pytest.mark.parametrize("body", ['return [{"symbol": "MSFT", "side": "buy", "quantity": 1}]', 'return [{"symbol": "AAPL", "side": "buy", "quantity": -1}]', 'return [{"symbol": "AAPL", "side": "buy", "quantity": 1, "cash": 999999}]'])
def test_invalid_order_intents_never_escape_the_boundary(image, body):
    from stock_quant.sandbox import SandboxSession
    data = fixture()
    bar = next(b for b in data.bars if b.timeframe == "15m")
    source = f'class Strategy:\n    def on_bar(self, ctx, bar):\n        {body}'
    with SandboxSession(artifact(source), symbols=["AAPL"], image=image) as session:
        with pytest.raises(ValueError, match="SANDBOX_POLICY_VIOLATION"):
            session.callback("on_bar", now=bar.available_at, bar=bar, snapshot=data, positions={}, equity=1000.)


@pytest.mark.parametrize("body", ['while True: pass', 'print("x" * 200000)', 'x = bytearray(300 * 1024 * 1024)'])
def test_cpu_output_and_memory_limits_fail_and_cleanup(image, body):
    from stock_quant.sandbox import SandboxSession
    source = 'class Strategy:\n    def initialize(self, ctx):\n        ' + body
    session = SandboxSession(artifact(source), symbols=["AAPL"], image=image, callback_seconds=.5)
    cid = session.container_id
    try:
        with pytest.raises((ValueError, TimeoutError), match="RESOURCE_LIMIT_EXCEEDED|SANDBOX_EXECUTION_FAILED"):
            session.callback("initialize", now=T, snapshot=fixture(), positions={}, equity=1000.)
    finally:
        session.close()
    assert subprocess.run(["docker", "inspect", cid], capture_output=True).returncode != 0


def test_cancellation_during_guest_execution_propagates_and_cleans_up(image):
    from stock_quant.sandbox import SandboxSession
    reached = False
    session = None
    def checkpoint():
        nonlocal reached
        if session is not None and b"entered-loop" in session.stderr_tail:
            reached = True
            raise InterruptedError("ownership lost")
    with SandboxSession(artifact('import os\nclass Strategy:\n    def initialize(self, ctx):\n        os.write(2, b"entered-loop\\n")\n        while True: pass'), symbols=["AAPL"], image=image, checkpoint=checkpoint) as session:
        cid = session.container_id
        with pytest.raises(InterruptedError, match="ownership lost"):
            session.callback("initialize", now=T, snapshot=fixture(), positions={}, equity=1000.)
    assert reached
    assert subprocess.run(["docker", "inspect", cid], capture_output=True).returncode != 0


def test_process_count_and_scratch_file_size_are_enforced(image):
    from stock_quant.sandbox import SandboxSession
    source = '''
import os, signal, time, resource
class Strategy:
    def initialize(self, ctx):
        assert resource.getrlimit(resource.RLIMIT_CPU) == (30, 30)
        children = []
        denied = False
        try:
            for i in range(40):
                try:
                    pid = os.fork()
                except OSError:
                    denied = True
                    break
                if pid == 0:
                    time.sleep(10)
                    os._exit(0)
                children.append(pid)
        finally:
            for pid in children:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
        assert denied and len(children) <= 31
        try:
            with open("/tmp/large", "wb") as stream:
                stream.write(b"x" * (2 * 1024 * 1024))
        except OSError:
            pass
        else:
            raise AssertionError("file quota ignored")
        denied = False
        for i in range(20):
            try:
                with open("/tmp/chunk" + str(i), "wb") as stream:
                    stream.write(b"x" * 500000)
            except OSError:
                denied = True
                break
        assert denied, "tmpfs aggregate quota ignored"
'''
    with SandboxSession(artifact(source), symbols=["AAPL"], image=image) as session:
        session.callback("initialize", now=T, snapshot=fixture(), positions={}, equity=1000.)


def test_detached_watchdog_removes_container_after_worker_sigkill(image):
    import sys
    from pathlib import Path
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    source = '''
from stock_quant.sandbox import PythonArtifact, SandboxSession
import time
session = SandboxSession(PythonArtifact(name="watchdog", source="class Strategy: pass"), symbols=["AAPL"], wall_seconds=2)
print(session.container_id, flush=True)
time.sleep(30)
'''
    process = subprocess.Popen([sys.executable, "-c", source], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    cid = None
    try:
        import selectors
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            assert ready.select(8), "worker did not publish container ID"
        cid = process.stdout.readline().strip()
        assert len(cid) == 64
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if subprocess.run(["docker", "inspect", cid], capture_output=True).returncode != 0:
                break
            time.sleep(.05)
        else:
            pytest.fail("watchdog left an orphan container")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
        if cid:
            subprocess.run(["docker", "rm", "--force", cid], capture_output=True, timeout=5)


def test_uncertain_create_response_does_not_leave_an_untracked_container(image, monkeypatch):
    from stock_quant.sandbox import SandboxSession
    original = SandboxSession._docker
    created = []
    def lost_response(*args):
        value = original(*args)
        if args[0] == "create":
            created.append(value.strip())
            raise ValueError("SANDBOX_UNAVAILABLE: simulated lost create response")
        return value
    monkeypatch.setattr(SandboxSession, "_docker", staticmethod(lost_response))
    try:
        with pytest.raises(ValueError, match="simulated lost create"):
            SandboxSession(artifact("class Strategy: pass"), symbols=["AAPL"], image=image)
        assert created
        assert subprocess.run(["docker", "inspect", created[0]], capture_output=True).returncode != 0
    finally:
        for cid in created:
            subprocess.run(["docker", "rm", "--force", cid], capture_output=True, timeout=5)


@pytest.mark.parametrize("fail_command", ["inspect", "rm"])
def test_watchdog_retries_transient_daemon_failures_until_removal(image, monkeypatch, fail_command):
    from stock_quant.sandbox import SandboxSession
    from stock_quant.sandbox_watchdog import reap
    original = subprocess.run
    failed = False
    with SandboxSession(artifact("class Strategy: pass"), symbols=["AAPL"], image=image) as session:
        cid = session.container_id
        def transient(command, *args, **kwargs):
            nonlocal failed
            if command[:2] == ["docker", fail_command] and not failed:
                failed = True
                return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"Cannot connect to Docker daemon")
            return original(command, *args, **kwargs)
        monkeypatch.setattr(subprocess, "run", transient)
        reap(cid, time.time())
        assert original(["docker", "inspect", cid], capture_output=True).returncode != 0


def test_fill_schema_and_universe_are_checked_before_guest_receives_it(image):
    from stock_quant.sandbox import SandboxSession
    source = 'class Strategy:\n    def on_fill(self, ctx, fill):\n        raise AssertionError("unauthorized fill reached guest")'
    fill = {"order_id": "order_1", "symbol": "MSFT", "side": "buy", "quantity": 1., "price": 10., "commission": 0., "time": T.isoformat()}
    for change in ({}, {"symbol": "AAPL", "account_secret": "must-not-cross"}):
        with SandboxSession(artifact(source), symbols=["AAPL"], image=image) as session:
            with pytest.raises(ValueError, match="SANDBOX_POLICY_VIOLATION"):
                session.callback("on_fill", now=T, fill={**fill, **change}, snapshot=fixture(), positions={}, equity=1000.)


def test_watchdog_does_not_treat_absence_as_proof_pending_create_cannot_commit(image, monkeypatch):
    from stock_quant.sandbox_watchdog import reap
    from uuid import uuid4
    name = "stock-quant-" + uuid4().hex
    original = subprocess.run
    observed_absence = False
    created = False

    def delayed_commit(command, *args, **kwargs):
        nonlocal observed_absence, created
        response = original(command, *args, **kwargs)
        if command == ["docker", "inspect", name] and response.returncode != 0 and not created:
            assert b"no such" in response.stderr.lower()
            observed_absence = True
            # Commit the one pending request immediately after the absence
            # observation, before reap can decide to relinquish ownership.
            creation = original(["docker", "create", "--name", name, "--label", "stock-quant.sandbox=v1",
                                 "--network", "none", image, "true"], capture_output=True, timeout=5)
            assert creation.returncode == 0, creation.stderr
            created = True
        return response

    monkeypatch.setattr(subprocess, "run", delayed_commit)
    try:
        reap(name, time.time(), settled=False)
        assert observed_absence and created
        assert original(["docker", "inspect", name], capture_output=True).returncode != 0
    finally:
        original(["docker", "rm", "--force", name], capture_output=True, timeout=5)


def test_create_acknowledgement_allows_absent_watchdog_to_exit(monkeypatch):
    from stock_quant.sandbox_watchdog import reap
    from uuid import uuid4
    read, write = os.pipe()
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout=b"", stderr=b"No such container"))
    try:
        os.write(write, b"1")
        reap("stock-quant-" + uuid4().hex, time.time(), settled=False, settled_fd=read)
    finally:
        os.close(write)


def test_successful_absent_rm_does_not_settle_uncertain_create(image, monkeypatch):
    from stock_quant.sandbox import SandboxSession
    original_docker, original_popen = SandboxSession._docker, subprocess.Popen
    pending, watchers = [], []
    def create_pending(*args):
        if args[0] == "create":
            pending.append(args)
            raise ValueError("SANDBOX_UNAVAILABLE: delayed create response")
        if args[0] == "rm":
            return ""  # Docker may report success although no target exists.
        return original_docker(*args)
    def track_watcher(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        if "stock_quant.sandbox_watchdog" in args[0]:
            watchers.append(process)
        return process
    monkeypatch.setattr(SandboxSession, "_docker", staticmethod(create_pending))
    monkeypatch.setattr(subprocess, "Popen", track_watcher)
    cid = None
    try:
        with pytest.raises(ValueError, match="delayed create"):
            SandboxSession(artifact("class Strategy: pass"), symbols=["AAPL"], image=image, wall_seconds=.1)
        assert watchers and watchers[0].poll() is None
        cid = original_docker(*pending[0]).strip()
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if subprocess.run(["docker", "inspect", cid], capture_output=True).returncode != 0:
                break
            time.sleep(.05)
        else:
            pytest.fail("uncertain create outlived its cleanup owner")
        watchers[0].wait(timeout=5)
    finally:
        if cid:
            subprocess.run(["docker", "rm", "--force", cid], capture_output=True, timeout=5)
        for process in watchers:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
