"""pytest plugin: run every test in its own forked child (no state shared
between tests, like the line map), recording pass/fail to $BRPROF_OUT/status_*.txt."""
import os

import pytest
from _pytest.runner import runtestprotocol


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    out = os.environ.get("BRPROF_OUT", "/tmp/vprof")
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            reports = runtestprotocol(item, nextitem=None, log=False)
            bad = any(r.failed or (r.when == "call" and r.skipped) for r in reports)
            with open(os.path.join(out, f"status_{os.getpid()}.txt"), "a") as f:
                f.write(f"{item.nodeid}\t{'FAIL' if bad else 'OK'}\n")
            code = 0
        finally:
            os._exit(code)
    _, st = os.waitpid(pid, 0)
    if st != 0:
        with open(os.path.join(out, f"status_parent_{os.getpid()}.txt"), "a") as f:
            f.write(f"{item.nodeid}\tCRASH\n")
    return True
