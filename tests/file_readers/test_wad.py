import random
import struct
import zlib

import pytest

from wizwalker.file_readers.cache_handler import CacheHandler
from wizwalker.file_readers.wad import Wad


def build_kiwad(path, files: dict[str, tuple[bytes, bool]]):
    """Write a version 2 KIWAD holding `files`: name -> (data, compress it)."""
    journal = b""
    stored = []
    for name, (data, compress) in files.items():
        stored.append(zlib.compress(data) if compress else data)

    header_size = 5 + 4 + 4 + 1
    journal_size = sum(4 * 3 + 1 + 4 + 4 + len(name) + 1 for name in files)
    offset = header_size + journal_size
    for (name, (data, compress)), blob in zip(files.items(), stored):
        encoded = name.encode() + b"\x00"
        zsize = len(blob) if compress else 0
        journal += struct.pack("<lll?ll", offset, len(data), zsize, compress, 0, len(encoded)) + encoded
        offset += len(blob)

    path.write_bytes(b"KIWAD" + struct.pack("<ll", 2, len(files)) + b"\x00" + journal + b"".join(stored))
    return path


@pytest.fixture
def incompressible() -> bytes:
    return random.Random(101).randbytes(300)


@pytest.fixture
def wad(tmp_path, incompressible) -> Wad:
    path = build_kiwad(
        tmp_path / "Test.wad",
        {
            "plain.txt": (b"plain text", False),
            "Locale/en-US/Spells.lang": (b"spell " * 100, True),
            "noise.bin": (incompressible, True),
        },
    )
    return Wad(path)


async def test_lists_its_files(wad):
    assert await wad.names() == ["plain.txt", "Locale/en-US/Spells.lang", "noise.bin"]
    assert wad.name == "Test"


async def test_reads_a_stored_and_a_compressed_file(wad):
    assert await wad.get_file("plain.txt") == b"plain text"
    assert await wad.get_file("Locale/en-US/Spells.lang") == b"spell " * 100


async def test_reads_a_file_that_compression_made_larger(wad, incompressible):
    # Read by its own size, this came back as its cut-off compressed bytes
    info = await wad.get_file_info("noise.bin")
    assert info.compressed_size > info.size
    assert await wad.get_file("noise.bin") == incompressible


async def test_an_unknown_file_is_a_value_error(wad):
    with pytest.raises(ValueError):
        await wad.get_file("missing.txt")
    with pytest.raises(ValueError):
        await wad.get_file_info("missing.txt")


async def test_a_missing_archive_is_a_value_error(tmp_path):
    with pytest.raises(ValueError):
        Wad(tmp_path / "Nothing.wad")


async def test_unarchives_into_folders(wad, tmp_path, incompressible):
    out = tmp_path / "out"
    out.mkdir()
    await wad.unarchive(out)
    assert (out / "Locale" / "en-US" / "Spells.lang").read_bytes() == b"spell " * 100
    assert (out / "noise.bin").read_bytes() == incompressible


async def test_archiving_a_folder_is_not_implemented(tmp_path):
    with pytest.raises(NotImplementedError):
        await Wad.from_directory(tmp_path)


def test_parses_a_lang_file():
    text = "Header:Spells\r\nFire_00\r\ncomment\r\nFire Cat\r\nIce_01\r\n\r\nIce Beetle"
    assert CacheHandler._parse_lang_file(text.encode("utf-16")) == {
        "Spells": {"Fire_00": "Fire Cat", "Ice_01": "Ice Beetle"}
    }


def test_an_undecodable_lang_file_parses_to_nothing():
    assert CacheHandler._parse_lang_file(b"\x00") is None
