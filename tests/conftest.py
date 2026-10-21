import pytest

from wizwalker.memory.handler import HookHandler
from wizwalker.memory.memory_reader import MemoryReader

from .fakes import FakeProcess


@pytest.fixture
def process(monkeypatch) -> FakeProcess:
    # is_running asks Windows about the process handle, which here is not one
    monkeypatch.setattr(MemoryReader, "is_running", lambda self: not self.process.closed)
    return FakeProcess()


@pytest.fixture
def hook_handler(process) -> HookHandler:
    return HookHandler(process, client=None)
