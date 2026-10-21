import pytest

from wizwalker import AddressOutOfRange, ClientClosedError, MemoryReadError, MemoryWriteError, utils
from wizwalker.constants import Primitive
from wizwalker.memory.memory_reader import MemoryReader


@pytest.fixture
def reader(process) -> MemoryReader:
    return MemoryReader(process)


async def test_read_bytes_returns_what_is_mapped(reader, process):
    process.map(0x1000, b"abcdef")
    assert await reader.read_bytes(0x1002, 3) == b"cde"


@pytest.mark.parametrize("address", [0, -8, 0x8000_0000_0000_0000])
async def test_read_bytes_refuses_an_address_out_of_range(reader, address):
    with pytest.raises(AddressOutOfRange):
        await reader.read_bytes(address, 4)


async def test_an_unmapped_read_is_a_memory_read_error(reader):
    with pytest.raises(MemoryReadError):
        await reader.read_bytes(0x5000, 4)


async def test_a_read_after_the_process_exited_is_client_closed(reader, process):
    process.map(0x1000, b"abcd")
    process.close_process()
    with pytest.raises(ClientClosedError):
        await reader.read_bytes(0x1000, 4)


async def test_an_unmapped_write_is_a_memory_write_error(reader):
    with pytest.raises(MemoryWriteError):
        await reader.write_bytes(0x5000, b"\x01")


async def test_typed_values_round_trip(reader, process):
    process.map(0x1000, size=16)
    await reader.write_typed(0x1000, -2, Primitive.int32)
    await reader.write_typed(0x1008, 1.5, Primitive.float64)
    assert await reader.read_typed(0x1000, Primitive.int32) == -2
    assert await reader.read_typed(0x1008, Primitive.float64) == 1.5


class Module:
    lpBaseOfDll = 0x7FFA_0000_0000


@pytest.fixture
def loaded(monkeypatch):
    """Every module counts as loaded in the fake process, at Module's base."""
    import pymem.process

    monkeypatch.setattr(pymem.process, "module_from_name", lambda handle, name: Module())


async def test_finds_a_symbol_in_a_system_dll(reader, loaded):
    address = await reader.get_address_from_symbol("user32.dll", "GetCursorPos")
    assert address > Module.lpBaseOfDll


async def test_takes_the_module_folder_as_a_str(reader, loaded):
    # It did module_dir / module_name, which a str can't
    folder = str(utils.get_system_directory())
    assert await reader.get_address_from_symbol("user32.dll", "GetCursorPos", module_dir=folder) > 0


async def test_an_unknown_module_or_symbol_is_a_value_error(reader, loaded, tmp_path):
    with pytest.raises(ValueError):
        await reader.get_address_from_symbol("user32.dll", "GetCursorPos", module_dir=str(tmp_path))
    with pytest.raises(ValueError):
        await reader.get_address_from_symbol("user32.dll", "NoSuchExport")


async def test_a_module_not_loaded_in_the_process_is_a_value_error(reader, monkeypatch):
    import pymem.process

    monkeypatch.setattr(pymem.process, "module_from_name", lambda handle, name: None)
    with pytest.raises(ValueError):
        await reader.get_address_from_symbol("user32.dll", "GetCursorPos")


async def test_allocate_and_free_go_to_the_process(reader, process):
    address = await reader.allocate(10)
    await reader.write_bytes(address, b"x" * 10)
    await reader.free(address)
    assert process.freed == [address]
