import asyncio
import functools
import regex
import struct
import threading
import weakref
from collections import OrderedDict
from pathlib import Path
from typing import Any, NamedTuple, Union

import pefile
import pymem
import pymem.exception
import pymem.process
import pymem.ressources.structure

from wizwalker import (
    AddressOutOfRange,
    ClientClosedError,
    MemoryReadError,
    MemoryWriteError,
    PatternFailed,
    PatternMultipleResults,
    Primitive,
    utils,
)


# Export tables by DLL path. A system DLL's exports are the same for every process,
# so one table serves every client.
_SYMBOL_TABLES: dict[str, dict[str, int]] = {}


class _ModuleMatch(NamedTuple):
    """One module scan result, as an offset from the module's base.

    `before` and `after` are how many bytes around it a check reads: enough context
    for a lookbehind, and a window the pattern matched in completely.
    """

    offset: int
    before: int
    after: int


# Module scan results by (module name, build, pattern, return_multiple). Every client
# of one build runs the same image, so a code pattern sits at the same offset from
# the module's base in each: only the base moves. A hit is checked in the client at
# hand before it is used, so a hooked site or a match in data that differs between
# processes is scanned for again.
_MODULE_SCANS: OrderedDict[tuple, tuple[_ModuleMatch, ...]] = OrderedDict()
_MODULE_SCANS_LOCK = threading.Lock()
# A key per pattern a build is scanned for, so a few dozen; the bound only keeps a
# session that sees many builds or patterns from growing without end
_MODULE_SCAN_LIMIT = 256
# Checking a hit costs a read per result, so a pattern with more results than this
# (instance_finder's every jmp) is cheaper to scan for again
_MAX_CACHED_RESULTS = 64
# How much before a result a check reads, for a lookbehind
_CONTEXT_BEFORE = 0x100
# The windows after a result tried in turn, until the pattern matches completely in one
_MATCH_WINDOWS = (0x40, 0x100, 0x400, 0x1000)
# The headers' page, which holds the PE fields the build is told by
_HEADER_SIZE = 0x1000

# The scan running for each key, as {loop: {key: event}}: an asyncio.Event belongs to
# one loop, and a loop that is gone takes its entry with it
_IN_FLIGHT: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def clear_module_scan_cache():
    """
    Forget every module scan result remembered across clients
    """
    with _MODULE_SCANS_LOCK:
        _MODULE_SCANS.clear()


def _cached_module_scan(key: tuple) -> tuple[_ModuleMatch, ...] | None:
    with _MODULE_SCANS_LOCK:
        matches = _MODULE_SCANS.get(key)
        if matches is not None:
            _MODULE_SCANS.move_to_end(key)
        return matches


def _cache_module_scan(key: tuple, matches: tuple[_ModuleMatch, ...]):
    with _MODULE_SCANS_LOCK:
        # An entry already there was checked good in some client; one client's
        # different answer is no reason to take it from the others
        if key in _MODULE_SCANS:
            return
        _MODULE_SCANS[key] = matches
        while len(_MODULE_SCANS) > _MODULE_SCAN_LIMIT:
            _MODULE_SCANS.popitem(last=False)


def _in_flight_scans() -> dict[tuple, asyncio.Event]:
    loop = asyncio.get_running_loop()
    with _MODULE_SCANS_LOCK:
        return _IN_FLIGHT.setdefault(loop, {})


class MemoryReader:
    """
    Represents anything that needs to read/write from/to memory
    """

    def __init__(self, process: pymem.Pymem):
        self.process = process

    # TODO: 2.0 make this a property
    def is_running(self) -> bool:
        """
        If the process we're reading/writing to/from is running
        """
        return utils.check_if_process_running(self.process.process_handle)

    @staticmethod
    async def run_in_executor(func, *args, **kwargs):
        """
        Run a function within an executor

        Args:
            func: The function to run
            args: Args to pass to the function
            kwargs: Kwargs to pass to the function
        """
        loop = asyncio.get_event_loop()
        function = functools.partial(func, *args, **kwargs)

        return await loop.run_in_executor(None, function)

    def _get_symbols(self, file_path: str, *, force_reload: bool = False):
        # Shared by every reader: a hook is a new reader each time it is activated,
        # so a per-instance table re-parsed user32.dll on every mouseless activation.
        key = str(file_path)
        if not force_reload and (dll_table := _SYMBOL_TABLES.get(key)) is not None:
            return dll_table

        # Only the export directory is needed. A full parse of user32.dll takes about
        # 200 ms of pure Python, holding the GIL; this takes a few.
        pe = pefile.PE(file_path, fast_load=True)
        try:
            pe.parse_data_directories(
                directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]]
            )

            symbols = {}

            for exp in pe.DIRECTORY_ENTRY_EXPORT.symbols:
                if exp.name:
                    symbols[exp.name.decode()] = exp.address

                else:
                    symbols[f"Ordinal {exp.ordinal}"] = exp.address
        finally:
            pe.close()

        _SYMBOL_TABLES[key] = symbols
        return symbols

    @staticmethod
    def _scan_page_return_all(handle, address, pattern):
        mbi = pymem.memory.virtual_query(handle, address)
        next_region = mbi.BaseAddress + mbi.RegionSize
        allowed_protections = [
            pymem.ressources.structure.MEMORY_PROTECTION.PAGE_EXECUTE_READ,
            pymem.ressources.structure.MEMORY_PROTECTION.PAGE_EXECUTE_READWRITE,
            pymem.ressources.structure.MEMORY_PROTECTION.PAGE_READWRITE,
            pymem.ressources.structure.MEMORY_PROTECTION.PAGE_READONLY,
        ]
        if (
            mbi.state != pymem.ressources.structure.MEMORY_STATE.MEM_COMMIT
            or mbi.protect not in allowed_protections
        ):
            return next_region, None

        page_bytes = pymem.memory.read_bytes(handle, address, mbi.RegionSize)

        found = []

        for match in regex.finditer(pattern, page_bytes, regex.DOTALL):
            found_address = address + match.span()[0]
            found.append(found_address)

        return next_region, found

    def _scan_all(
        self,
        handle: int,
        pattern: bytes,
        return_multiple: bool = False,
    ):
        next_region = 0

        found = []
        while next_region < 0x7FFFFFFF0000:
            next_region, page_found = self._scan_page_return_all(
                handle, next_region, pattern
            )
            if page_found:
                found += page_found

            if not return_multiple and found:
                break

        return found

    def _scan_entire_module(self, handle, module, pattern):
        base_address = module.lpBaseOfDll
        max_address = module.lpBaseOfDll + module.SizeOfImage
        page_address = base_address

        found = []
        while page_address < max_address:
            page_address, page_found = self._scan_page_return_all(
                handle, page_address, pattern
            )
            if page_found:
                found += page_found

        return found

    def _module_build(self, module) -> tuple | None:
        """
        What tells this module's build from another's, read from its PE headers here,
        or None if they can't be read
        """
        # The headers in memory describe the image this process runs. The file on disk
        # doesn't: the patcher can replace it while older clients still run the old one.
        try:
            header = self.process.read_bytes(module.lpBaseOfDll, _HEADER_SIZE)
        except Exception:
            # Not a module whose headers can be read, so not one to cache for
            return None

        if header[:2] != b"MZ":
            return None
        (nt_headers,) = struct.unpack_from("<I", header, 0x3C)
        optional_header = nt_headers + 24
        if optional_header + 68 > len(header):
            return None
        if header[nt_headers : nt_headers + 4] != b"PE\0\0":
            return None

        # The linker stamps TimeDateStamp anew for each build (the link time, as the
        # game's is, or a hash of the image under /Brepro), and the rest change with
        # nearly any change to the code. The loader rewrites ImageBase, so nothing is
        # taken that differs between clients of one build.
        machine, _, time_date_stamp = struct.unpack_from("<HHI", header, nt_headers + 4)
        (entry_point,) = struct.unpack_from("<I", header, optional_header + 16)
        size_of_image, _, checksum = struct.unpack_from("<III", header, optional_header + 56)
        return machine, time_date_stamp, checksum, size_of_image, entry_point

    def _module_matches(
        self, module, pattern: bytes, found: list[int]
    ) -> tuple[_ModuleMatch, ...] | None:
        """
        A scan's results as offsets a client of the same build can check, or None if
        they're not worth caching or can't be checked by a read at each
        """
        if len(found) > _MAX_CACHED_RESULTS:
            return None

        compiled = regex.compile(pattern, regex.DOTALL)
        base = module.lpBaseOfDll
        end = base + module.SizeOfImage

        matches = []
        for address in found:
            if not base <= address < end:
                return None
            before = min(_CONTEXT_BEFORE, address - base)
            for window in _MATCH_WINDOWS:
                after = min(window, end - address)
                try:
                    data = self.process.read_bytes(address - before, before + after)
                except Exception:
                    return None
                match = compiled.match(data, before, partial=True)
                if match is None:
                    # The match needs more than the read shows, such as a lookbehind
                    # reaching further back: leave it to a scan, every time
                    return None
                if not match.partial:
                    break
                if after < window:
                    # Still unfinished at the module's end
                    return None
            else:
                return None
            matches.append(_ModuleMatch(address - base, before, after))

        return tuple(matches)

    def _checked_module_matches(
        self, module, pattern: bytes, matches: tuple[_ModuleMatch, ...]
    ) -> list[int] | None:
        """
        A cached scan's results as addresses in this process, or None unless the
        pattern still matches at every one of them
        """
        compiled = regex.compile(pattern, regex.DOTALL)
        base = module.lpBaseOfDll

        found = []
        for match in matches:
            address = base + match.offset
            try:
                data = self.process.read_bytes(address - match.before, match.before + match.after)
            except Exception:
                return None
            # A hook's jump replaces the bytes it was found by
            if compiled.match(data, match.before) is None:
                return None
            found.append(address)

        return found

    async def _scan_module(self, module, pattern: bytes) -> list[int]:
        # this can take a long time to run when collecting multiple results
        # so must be run in an executor
        return await self.run_in_executor(
            self._scan_entire_module,
            self.process.process_handle,
            module,
            pattern,
        )

    def _remember_module_scan(
        self, key: tuple, module, pattern: bytes, found: list[int], return_multiple: bool
    ):
        # Never a failure: no result, or more than one where one was asked for
        if not found or (len(found) > 1 and not return_multiple):
            return
        if (matches := self._module_matches(module, pattern, found)) is not None:
            _cache_module_scan(key, matches)

    async def _scan_module_cached(
        self, module_name: str, module, pattern: bytes, return_multiple: bool
    ) -> list[int]:
        """
        A module scan, from the results a client of the same build found when they
        still match here
        """
        build = self._module_build(module) if isinstance(pattern, bytes) else None
        if build is None:
            return await self._scan_module(module, pattern)

        key = (module_name.lower(), build, pattern, return_multiple)
        matches = _cached_module_scan(key)

        if matches is None:
            in_flight = _in_flight_scans()
            if (scanning := in_flight.get(key)) is not None:
                # Another client is scanning for it already: its results, checked
                # here, save this one the scan
                await scanning.wait()
                matches = _cached_module_scan(key)
            else:
                in_flight[key] = scanning = asyncio.Event()
                try:
                    found = await self._scan_module(module, pattern)
                    self._remember_module_scan(key, module, pattern, found, return_multiple)
                    return found
                finally:
                    del in_flight[key]
                    scanning.set()

        if matches is not None:
            found = self._checked_module_matches(module, pattern, matches)
            if found is not None:
                return found

        found = await self._scan_module(module, pattern)
        self._remember_module_scan(key, module, pattern, found, return_multiple)
        return found

    async def pattern_scan(
        self, pattern: bytes, *, module: str = None, return_multiple: bool = False
    ) -> Union[list, int]:
        """
        Scan for a pattern

        Args:
            pattern: The byte pattern to search for
            module: What module to search or None to search all
            return_multiple: If multiple results should be returned

        Raises:
            PatternFailed: If the pattern returned no results
            PatternMultipleResults: If the pattern returned multiple results and return_multple is False

        Returns:
            A list of results if return_multple is True otherwise one result
        """
        if module:
            module_object = pymem.process.module_from_name(self.process.process_handle, module)

            if module_object is None:
                raise ValueError(f"{module} module not found.")

            found_addresses = await self._scan_module_cached(
                module, module_object, pattern, return_multiple
            )

        else:
            found_addresses = await self.run_in_executor(
                self._scan_all,
                self.process.process_handle,
                pattern,
                return_multiple,
            )

        if (found_length := len(found_addresses)) == 0:
            raise PatternFailed(pattern)
        elif found_length > 1 and not return_multiple:
            raise PatternMultipleResults(f"Got {found_length} results for {pattern}")
        elif return_multiple:
            return found_addresses
        else:
            return found_addresses[0]

    async def get_address_from_symbol(
        self,
        module_name: str,
        symbol_name: str,
        *,
        module_dir: str = None,
        force_reload: bool = False,
    ) -> int:
        """
        Get an address from a module using its symbol

        Args:
            module_name: Name of the module
            symbol_name: Name of the symbol
            module_dir: Dir the module is within
            force_reload: Force export table reload

        Returns:
            The address of the symbol in memory

        Raises:
            ValueError: No symbol/module with that name
        """
        if not module_dir:
            module_dir = utils.get_system_directory()

        # The annotation says str, and a str has no / operator
        file_path = Path(module_dir) / module_name

        if not file_path.exists():
            raise ValueError(f"No module named {module_name}")

        symbols = await self.run_in_executor(
            self._get_symbols, file_path, force_reload=force_reload
        )

        if not (symbol := symbols.get(symbol_name)):
            raise ValueError(f"No symbol named {symbol_name} in module {module_name}")

        module = pymem.process.module_from_name(
            self.process.process_handle, module_name
        )
        if module is None:
            raise ValueError(f"Module {module_name} is not loaded in the process")

        return module.lpBaseOfDll + symbol

    async def allocate(self, size: int) -> int:
        """
        Allocate some bytes

        Args:
            size: The number of bytes to allocate

        Returns:
            The allocated address
        """
        return self.process.allocate(size)

    async def free(self, address: int):
        """
        Free some bytes

        Args:
             address: The address to free
        """
        self.process.free(address)

    # TODO: figure out how params works
    async def start_thread(self, address: int):
        """
        Start a thread at an address

        Args:
            address: The address to start the thread at
        """
        await self.run_in_executor(self.process.start_thread, address)

    async def read_bytes(self, address: int, size: int) -> bytes:
        """
        Read some bytes from memory

        Args:
            address: The address to read from
            size: The number of bytes to read

        Raises:
            ClientClosedError: If the client is closed
            MemoryReadError: If there was an error reading memory
            AddressOutOfRange: If the addrress is out of bounds
        """
        if not 0 < address <= 0x7FFFFFFFFFFFFFFF:
            raise AddressOutOfRange(address)

        try:
            return self.process.read_bytes(address, size)
        except pymem.exception.ProcessError:
            # the handle was released once the process exited
            raise ClientClosedError()
        except pymem.exception.MemoryReadError:
            # we don't want to run is running for every read
            # so we just check after we error
            if not self.is_running():
                raise ClientClosedError()
            else:
                raise MemoryReadError(address)

    async def write_bytes(self, address: int, value: bytes):
        """
        Write bytes to memory

        Args:
            address: The address to write to
            value: The bytes to write
        """
        size = len(value)

        try:
            self.process.write_bytes(address, value, size)
        except pymem.exception.ProcessError:
            # see read_bytes
            raise ClientClosedError()
        except pymem.exception.MemoryWriteError:
            # see read_bytes
            if not self.is_running():
                raise ClientClosedError()
            else:
                raise MemoryWriteError(address)

    async def read_typed(self, address: int, data_type: Primitive) -> Any:
        """
        Read typed bytes from memory

        Args:
            address: The address to read from
            data_type: The type to read (defined in constants)

        Returns:
            The converted data type
        """
        data = await self.read_bytes(address, data_type.value.size)
        return data_type.value.unpack(data)[0]

    async def write_typed(self, address: int, value: Any, data_type: Primitive):
        """
        Write typed bytes to memory

        Args:
            address: The address to write to
            value: The value to convert and then write
            data_type: The data type to convert to
        """
        packed_data = data_type.value.pack(value)
        await self.write_bytes(address, packed_data)
