import pytest

from wizwalker import AddressOutOfRange, ClientClosedError, MemoryReadError, MemoryWriteError
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


async def test_allocate_and_free_go_to_the_process(reader, process):
    address = await reader.allocate(10)
    await reader.write_bytes(address, b"x" * 10)
    await reader.free(address)
    assert process.freed == [address]
