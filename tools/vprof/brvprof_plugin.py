"""pytest plugin: tell the value profiler which test is running."""
import builtins

import pytest


def _set(item, phase):
    f = getattr(builtins, "__br_key_of__", None)
    if f is not None:
        builtins.__br_key__ = f(f"{item.nodeid} ({phase})")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _set(item, "setup")
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _set(item, "call")
    yield
    _set(item, "teardown")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    _set(item, "teardown")
    yield
