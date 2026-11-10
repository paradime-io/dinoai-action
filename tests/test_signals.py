"""The step's `run:` line must hand GitHub's cancel signal to Python.

Cancelling a job (a newer push with cancel-in-progress, or the Cancel button) signals the step's
process. If that is bash with Python as its child, bash does not pass the signal on, Python never
reaches `stop_run`, and the agent session keeps running in Paradime. This runs the exact `run:`
line from action.yml the way the runner does, with a stub in place of the real entrypoint.
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STUB = """\
import os, signal, sys, time

def _on_signal(signum, _frame):
    with open(os.environ["MARKER"], "w") as fh:
        fh.write(signal.Signals(signum).name)
    sys.exit(130)

signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)
open(os.environ["READY"], "w").close()
time.sleep(30)
"""


def _run_line() -> str:
    with open(os.path.join(ROOT, "action.yml")) as fh:
        lines = re.findall(r"^\s*run: (.*dinoai_action\.main.*)$", fh.read(), re.M)
    assert len(lines) == 1, lines
    return lines[0]


@unittest.skipIf(sys.platform == "win32" or shutil.which("bash") is None, "needs bash")
class CancelSignalTest(unittest.TestCase):
    def _deliver(self, signum: int) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            pkg = os.path.join(tmp, "dinoai_action")
            os.mkdir(pkg)
            open(os.path.join(pkg, "__init__.py"), "w").close()
            with open(os.path.join(pkg, "main.py"), "w") as fh:
                fh.write(STUB)
            script = os.path.join(tmp, "step.sh")
            with open(script, "w") as fh:
                fh.write(_run_line() + "\n")
            marker, ready = os.path.join(tmp, "marker"), os.path.join(tmp, "ready")
            env = {**os.environ, "PYTHONPATH": tmp, "MARKER": marker, "READY": ready}

            # Same shell as the runner uses for `shell: bash`.
            proc = subprocess.Popen(["bash", "--noprofile", "--norc", "-eo", "pipefail", script], cwd=tmp, env=env, start_new_session=True)
            try:
                deadline = time.monotonic() + 10
                while not os.path.exists(ready):
                    self.assertLess(time.monotonic(), deadline, "stub never started")
                    time.sleep(0.05)
                proc.send_signal(signum)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass  # bash sat on the signal; the marker check below reports it
            finally:
                # Signal the whole group: without `exec`, the stub outlives bash.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            if not os.path.exists(marker):
                return ""
            with open(marker) as fh:
                return fh.read()

    def test_sigterm_reaches_python(self):
        self.assertEqual(self._deliver(signal.SIGTERM), "SIGTERM")

    def test_sigint_reaches_python(self):
        self.assertEqual(self._deliver(signal.SIGINT), "SIGINT")


if __name__ == "__main__":
    unittest.main()
