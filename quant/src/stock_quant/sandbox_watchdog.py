"""Detached trusted cleanup survives a worker SIGKILL, bounded by a wall deadline."""
import json
import os
import subprocess
import sys
import time


def reap(container: str, deadline: float, *, settled: bool = True, settled_fd: int | None = None):
    time.sleep(max(0., deadline - time.time()))
    delay = 1.
    observed = False
    if settled_fd is not None:
        os.set_blocking(settled_fd, False)
    while True:
        if settled_fd is not None:
            try:
                notice = os.read(settled_fd, 1)
                if notice:
                    settled = notice == b"1"
                if notice or notice == b"":
                    os.close(settled_fd)
                    settled_fd = None
            except BlockingIOError:
                pass
        try:
            state = subprocess.run(["docker", "inspect", container], capture_output=True, timeout=5)
            if state.returncode != 0:
                # Daemon failures do not establish absence. Keep the cleanup
                # owner alive until removal is confirmed after recovery.
                error = state.stderr.decode(errors="replace").lower()
                if "no such object" in error or "no such container" in error:
                    # Absence is conclusive only after creation settled or we
                    # observed its committed object. A lost create response may
                    # still commit later, even after this deadline.
                    if settled or observed:
                        return
            else:
                record = json.loads(state.stdout)[0]
                if (record["Config"].get("Labels") or {}).get("stock-quant.sandbox") != "v1" or not record["Name"].startswith("/stock-quant-"):
                    return  # Never remove an unrelated daemon object.
                observed = True
                subprocess.run(["docker", "rm", "--force", container], capture_output=True, timeout=5)
                # A successful CLI response is followed by an absence check.
        except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
            pass
        time.sleep(delay)
        delay = min(30., delay * 2)


def main():
    reap(sys.argv[1], float(sys.argv[2]), settled=False, settled_fd=int(sys.argv[3]))


if __name__ == "__main__":
    main()
