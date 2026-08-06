"""Run the API, calendar simulator and worker; PostgreSQL must already be running."""

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    subprocess.run([sys.executable, "-m", "app.db"], cwd=ROOT, check=True)
    processes = []
    commands = [
        ["-m", "uvicorn", "app.calendar_simulator:app", "--host", "127.0.0.1", "--port", "8001"],
        ["-m", "app.worker"],
        [
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            os.getenv("PORT", "8010"),
            "--reload",
        ],
    ]
    try:
        for command in commands:
            processes.append(subprocess.Popen([sys.executable, *command], cwd=ROOT))
        print("Huddle: http://127.0.0.1:" + os.getenv("PORT", "8010"), flush=True)
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
        raise SystemExit("A service exited. Check the output above.")
    except KeyboardInterrupt:
        pass
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
