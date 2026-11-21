import pytest

from wizwalker.memory.handler import HookHandler
from wizwalker.memory.memory_reader import MemoryReader, clear_module_scan_cache

from .fakes import FakeProcess


@pytest.fixture(autouse=True)
def no_module_scans_from_other_tests():
    # Module scan results are kept for every reader in the process, and the fakes
    # lay out the same module at the same base in test after test
    clear_module_scan_cache()
    yield
    clear_module_scan_cache()


@pytest.fixture
def process(monkeypatch) -> FakeProcess:
    # is_running asks Windows about the process handle, which here is not one
    monkeypatch.setattr(MemoryReader, "is_running", lambda self: not self.process.closed)
    return FakeProcess()


@pytest.fixture
def hook_handler(process) -> HookHandler:
    return HookHandler(process, client=None)
