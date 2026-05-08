import os
import signal
import subprocess
import sys
import time


def _terminate(processes: list[tuple[str, subprocess.Popen]]) -> None:
    for _, proc in processes:
        if proc.poll() is None:
            proc.terminate()

    deadline = time.monotonic() + 10
    for _, proc in processes:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    port = os.getenv("PORT", "8080")
    host = os.getenv("HOST", "0.0.0.0")

    processes: list[tuple[str, subprocess.Popen]] = [
        ("bot", subprocess.Popen([sys.executable, "main.py"])),
        (
            "dashboard",
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "analytics.server:app",
                    "--host",
                    host,
                    "--port",
                    port,
                ]
            ),
        ),
    ]

    stopping = False

    def request_stop(signum, _frame) -> None:
        nonlocal stopping
        stopping = True
        print(f"Received signal {signum}; stopping Railway processes", flush=True)
        _terminate(processes)

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        while not stopping:
            for name, proc in processes:
                code = proc.poll()
                if code is not None:
                    print(f"{name} process exited with code {code}", flush=True)
                    _terminate(processes)
                    return code
            time.sleep(1)
    finally:
        _terminate(processes)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
