"""Log a client in from its login screen through the game's own login command.

``utils.instance_login`` types the credentials: the username, a tab, the password and
enter, into whichever field has focus. That rests on the login window's focus and tab
order, which the game's 2026-11-16 build moved, sending the two to the wrong fields.
This calls the command the login window itself ends in instead, so no change to the
window can reach it.

How, as wizlaunch does it (Deimos' launcher; Click found the call, Slackaduts ported it):

* The game's login code calls a dispatcher with a command string: ``rcx`` the game
  client, read through a global, ``rdx`` the string, ``r8d`` 0 and ``r9b`` 1.
  ``LOGIN_CALL_PATTERN`` finds that call, which gives both addresses.
* Calling it from another thread is not safe, so a stub on a site the main thread runs
  every frame makes the call once, between frames, and clears a flag to say it has. The
  site is the root window hook's, whose seven-byte load the stub replays, as that hook's
  does.
* The site is put back and the stub freed as soon as the flag clears.

The site cannot carry both: each writes a jump over the same seven bytes, and the one
that went second would replay the other's jump out of place. A site that holds a jump
already no longer matches the pattern, so this refuses rather than writes over it, and a
caller that hooks clients holds their hooks back until it is done.

The command is space-separated, so a username or password with a space in it cannot be
sent this way, nor one outside printable ASCII; :func:`login_command` refuses both.
"""

import asyncio
import ctypes
import ctypes.wintypes
import struct

import pymem
import pymem.exception

from wizwalker import MemoryLoginError, PatternFailed, PatternMultipleResults, utils
from wizwalker.memory.hooks import RootWindowHook
from wizwalker.memory.memory_reader import MemoryReader

MODULE = "WizardGraphicalClient.exe"

# mov r9b, 1 / xor r8d, r8d / lea rdx, [rbp-31h] / mov rcx, [rip+disp32] / call rel32.
# The game has it twice, and both copies call the same function with the same client.
LOGIN_CALL_PATTERN = (
    rb"\x41\xB1\x01\x45\x33\xC0\x48\x8D\x55\xCF\x48\x8B\x0D....\xE8...."
)
# Where each displacement sits in the match, and where the instruction it is relative to ends.
_CLIENT_DISP_AT, _CLIENT_DISP_FROM = 13, 17
_DISPATCH_DISP_AT, _DISPATCH_DISP_FROM = 18, 22

SITE_PATTERN = RootWindowHook.pattern
SITE_LENGTH = RootWindowHook.instruction_length

# One region holds the stub and its data, near enough the site for a rel32 jump.
REGION_SIZE = 0x1000
FLAG_OFFSET = 0x000
STRING_OFFSET = 0x010
CODE_OFFSET = 0x040
COMMAND_OFFSET = 0x200
MAX_COMMAND = REGION_SIZE - COMMAND_OFFSET - 1

ARMED = 1
DONE = 0

# How long the site is left alone after it is put back, before the region under it is
# freed: the main thread may be partway through the stub's last instructions.
SETTLE_DELAY = 0.25
POLL_INTERVAL = 0.05

# rel32 reaches 2 GB either way; VirtualAllocEx places a region on a 64 KB boundary.
_JUMP_RANGE = 0x7FFF_0000
_ALLOCATION_GRANULARITY = 0x1_0000
_MEM_COMMIT_RESERVE = 0x1000 | 0x2000
_PAGE_EXECUTE_READWRITE = 0x40


def login_command(username: str, password: str) -> bytes:
    """The command the login window sends, ``login <username> <password>``, null-terminated."""
    for value in (username, password):
        if not value or not value.isascii() or not value.isprintable() or " " in value:
            raise MemoryLoginError(
                "a credential is empty, has a space, or is not printable ASCII"
            )
    command = f"login {username} {password}".encode("ascii")
    if len(command) > MAX_COMMAND:
        raise MemoryLoginError("the credentials are too long")
    return command + b"\x00"


def string_struct(data_address: int, length: int) -> bytes:
    """An MSVC std::string of *length* chars at *data_address*.

    The capacity says heap, whatever the length: at 15 or under, MSVC reads the first 16
    bytes as the characters themselves, and those are the pointer.
    """
    return struct.pack("<QQQQ", data_address, 0, length, max(length, 16))


def resolve_login_call(match_address: int, operands: bytes) -> tuple[int, int]:
    """The global holding the game client, and the dispatcher, from one pattern match.

    *operands* are the nine bytes from the first displacement on: disp32, the call's
    opcode, disp32.
    """
    client_disp = struct.unpack_from("<i", operands, 0)[0]
    dispatch_disp = struct.unpack_from(
        "<i", operands, _DISPATCH_DISP_AT - _CLIENT_DISP_AT
    )[0]
    return (
        match_address + _CLIENT_DISP_FROM + client_disp,
        match_address + _DISPATCH_DISP_FROM + dispatch_disp,
    )


def _rel32(from_end: int, to: int) -> bytes:
    """A rel32 from the end of the instruction at *from_end* to *to*."""
    distance = to - from_end
    if not -(2**31) <= distance < 2**31:
        raise MemoryLoginError("the stub is out of jump range of the site")
    return struct.pack("<i", distance)


def build_stub(
    *,
    code_address: int,
    flag_address: int,
    string_address: int,
    client_global: int,
    dispatch: int,
    original: bytes,
    return_address: int,
) -> bytes:
    """The code the site jumps to.

    While the flag is armed it calls the dispatcher with the command and clears the flag;
    either way it then replays the site's own instruction and jumps back after it. Seven
    pushes and the 0x28 of shadow space keep the stack as aligned as the site found it,
    which the call the site is about to make needs it to be already.
    """
    # fmt: off
    check = (
        b"\x50"                                                 # push rax
        b"\x48\xB8" + struct.pack("<Q", flag_address) +         # mov rax, flag
        b"\x80\x38" + bytes([ARMED]) +                          # cmp byte [rax], ARMED
        b"\x58"                                                 # pop rax
    )
    call = (
        b"\x50\x51\x52\x41\x50\x41\x51\x41\x52\x41\x53"         # push rax rcx rdx r8 r9 r10 r11
        b"\x48\x83\xEC\x28"                                     # sub rsp, 0x28
        b"\x41\xB1\x01"                                         # mov r9b, 1
        b"\x45\x33\xC0"                                         # xor r8d, r8d
        b"\x48\xBA" + struct.pack("<Q", string_address) +       # mov rdx, string
        b"\x48\xB8" + struct.pack("<Q", client_global) +        # mov rax, client global
        b"\x48\x8B\x08"                                         # mov rcx, [rax]
        b"\x48\xB8" + struct.pack("<Q", dispatch) +             # mov rax, dispatch
        b"\xFF\xD0"                                             # call rax
        b"\x48\xB8" + struct.pack("<Q", flag_address) +         # mov rax, flag
        b"\xC6\x00" + bytes([DONE]) +                           # mov byte [rax], DONE
        b"\x48\x83\xC4\x28"                                     # add rsp, 0x28
        b"\x41\x5B\x41\x5A\x41\x59\x41\x58\x5A\x59\x58"         # pop r11 r10 r9 r8 rdx rcx rax
    )
    # fmt: on
    skip = b"\x0f\x85" + struct.pack("<i", len(call))  # jne past the call
    head = check + skip + call + original
    jump_back = b"\xe9" + _rel32(code_address + len(head) + 5, return_address)
    return head + jump_back


def site_jump(site: int, code_address: int, length: int = SITE_LENGTH) -> bytes:
    """The jump written over the site: E9 rel32, padded to the instruction it replaces."""
    return b"\xe9" + _rel32(site + 5, code_address) + b"\x90" * (length - 5)


def _virtual_alloc_ex():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.VirtualAllocEx
    function.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.LPVOID,
        ctypes.c_size_t,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
    ]
    function.restype = ctypes.wintypes.LPVOID
    return function


def allocate_near(reader: MemoryReader, near: int, size: int) -> int:
    """Executable memory within rel32 reach of *near*, tried a granule at a time outward."""
    virtual_alloc_ex = _virtual_alloc_ex()
    base = near & ~(_ALLOCATION_GRANULARITY - 1)
    for offset in range(_ALLOCATION_GRANULARITY, _JUMP_RANGE, _ALLOCATION_GRANULARITY):
        for candidate in (base + offset, base - offset):
            if candidate <= 0:
                continue
            address = virtual_alloc_ex(
                reader.process.process_handle,
                candidate,
                size,
                _MEM_COMMIT_RESERVE,
                _PAGE_EXECUTE_READWRITE,
            )
            if address:
                return address
    raise MemoryLoginError("no memory free within jump range of the site")


async def _find_login_call(reader: MemoryReader) -> tuple[int, int]:
    try:
        matches = await reader.pattern_scan(
            LOGIN_CALL_PATTERN, module=MODULE, return_multiple=True
        )
    except PatternFailed:
        raise MemoryLoginError("the login call was not found") from None
    resolved = set()
    for match in matches:
        operands = await reader.read_bytes(
            match + _CLIENT_DISP_AT, _DISPATCH_DISP_FROM - _CLIENT_DISP_AT
        )
        resolved.add(resolve_login_call(match, operands))
    if len(resolved) != 1:
        raise MemoryLoginError(f"the login call's {len(matches)} matches disagree")
    return resolved.pop()


async def _find_site(reader: MemoryReader) -> int:
    try:
        return await reader.pattern_scan(SITE_PATTERN, module=MODULE)
    except PatternFailed:
        # Also what a site that is hooked already looks like: its load is a jump now.
        raise MemoryLoginError(
            "the root window site was not found, or is hooked already"
        ) from None
    except PatternMultipleResults:
        raise MemoryLoginError("the root window site matched more than once") from None


async def login_through(
    reader: MemoryReader,
    username: str,
    password: str,
    *,
    timeout: float = 10.0,
    allocate=allocate_near,
) -> None:
    """Send the login command through *reader*'s process, waiting for the game to run it.

    Raises :class:`MemoryLoginError` when it cannot be sent, or when the game did not run
    it within *timeout*; either way the site is as it was found. *allocate* is
    ``allocate(reader, near, size)``, for the tests.
    """
    command = login_command(username, password)
    client_global, dispatch = await _find_login_call(reader)
    site = await _find_site(reader)
    original = await reader.read_bytes(site, SITE_LENGTH)

    region = await reader.run_in_executor(allocate, reader, site, REGION_SIZE)
    flag = region + FLAG_OFFSET
    code = region + CODE_OFFSET
    string = region + STRING_OFFSET
    data = region + COMMAND_OFFSET
    patched = False
    try:
        await reader.write_bytes(data, command)
        await reader.write_bytes(string, string_struct(data, len(command) - 1))
        await reader.write_bytes(flag, bytes([ARMED]))
        await reader.write_bytes(
            code,
            build_stub(
                code_address=code,
                flag_address=flag,
                string_address=string,
                client_global=client_global,
                dispatch=dispatch,
                original=original,
                return_address=site + SITE_LENGTH,
            ),
        )
        await reader.write_bytes(site, site_jump(site, code))
        patched = True

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (await reader.read_bytes(flag, 1))[0] != DONE:
            if loop.time() >= deadline:
                raise MemoryLoginError(
                    f"the game did not run the login within {timeout:.0f}s"
                )
            await asyncio.sleep(POLL_INTERVAL)
    finally:
        # Disarmed first, so a frame that reaches the stub before the site is back skips the call.
        await reader.write_bytes(flag, bytes([DONE]))
        if patched:
            await reader.write_bytes(site, original)
            await asyncio.sleep(SETTLE_DELAY)
        # The password stays in the game's memory no longer than it has to.
        await reader.write_bytes(data, b"\x00" * len(command))
        await reader.free(region)


async def memory_login(
    window_handle: int, username: str, password: str, *, timeout: float = 10.0
) -> None:
    """Log the client in *window_handle* in, from its login screen. See the module docstring.

    Raises :class:`MemoryLoginError` when the login cannot be sent this way, and the
    caller may type it instead (``utils.instance_login``).
    """
    login_command(username, password)  # refuse before opening anything
    pid = utils.get_pid_from_handle(window_handle)
    if not pid:
        raise MemoryLoginError("the window has no process")
    process = pymem.Pymem()
    try:
        process.open_process_from_id(pid)
    except pymem.exception.ProcessError as error:
        raise MemoryLoginError(
            f"the game's process could not be opened: {error}"
        ) from None
    try:
        await login_through(MemoryReader(process), username, password, timeout=timeout)
    finally:
        process.close_process()
