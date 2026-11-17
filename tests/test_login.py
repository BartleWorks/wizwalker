"""wizwalker.login: the login command, sent through a stub on the root window site."""

import asyncio
import struct
from types import SimpleNamespace

import pymem.process
import pytest
import regex

from wizwalker import MemoryLoginError, login
from wizwalker.memory.memory_reader import MemoryReader

# Below the fake's allocations, and within rel32 reach of them.
MODULE_BASE = 0x7EFF_FF00_0000
MODULE_SIZE = 0x1000
CLIENT_GLOBAL = MODULE_BASE + 0x800
DISPATCH = MODULE_BASE + 0x900
SITE = MODULE_BASE + 0x300
# The root window's load and the vtable call after it, as the 2026-11-16 client has them.
LOAD = bytes.fromhex("49 8b 8f d8 00 00 00")
SITE_BYTES = LOAD + bytes.fromhex("48 8b 01 49 8b 97 c8 00 00 00 ff 50 70 84 db")


def _login_call(
    at: int, client_global: int = CLIENT_GLOBAL, dispatch: int = DISPATCH
) -> bytes:
    return (
        bytes.fromhex("41 b1 01 45 33 c0 48 8d 55 cf 48 8b 0d")
        + struct.pack("<i", client_global - (at + 17))
        + b"\xe8"
        + struct.pack("<i", dispatch - (at + 22))
    )


@pytest.fixture
def module(process, monkeypatch):
    """The client's module with both copies of the login call and the root window site."""
    process.map(MODULE_BASE, size=MODULE_SIZE)
    for at in (MODULE_BASE + 0x100, MODULE_BASE + 0x200):
        process.write_bytes(at, _login_call(at), 22)
    process.write_bytes(SITE, SITE_BYTES, len(SITE_BYTES))

    monkeypatch.setattr(
        pymem.process,
        "module_from_name",
        lambda handle, name: SimpleNamespace(
            lpBaseOfDll=MODULE_BASE, SizeOfImage=MODULE_SIZE
        ),
    )

    def scan(self, handle, module_object, pattern):
        page = self.process.read_bytes(
            module_object.lpBaseOfDll, module_object.SizeOfImage
        )
        return [
            module_object.lpBaseOfDll + m.start()
            for m in regex.finditer(pattern, page, regex.DOTALL)
        ]

    monkeypatch.setattr(MemoryReader, "_scan_entire_module", scan)
    monkeypatch.setattr(login, "SETTLE_DELAY", 0)
    monkeypatch.setattr(login, "POLL_INTERVAL", 0.001)
    return process


def _allocate(reader, near, size):
    return reader.process.allocate(size)


async def _frames(process, seen: list):
    """The game's main thread: each frame that finds the site jumping, runs the armed stub."""
    while True:
        head = process.read_bytes(SITE, 5)
        if head[0] == 0xE9:
            code = SITE + 5 + struct.unpack("<i", head[1:])[0]
            region = code - login.CODE_OFFSET
            if process.read_bytes(region + login.FLAG_OFFSET, 1)[0] == login.ARMED:
                data, _, length, _ = struct.unpack(
                    "<QQQQ", process.read_bytes(region + login.STRING_OFFSET, 32)
                )
                seen.append((region, process.read_bytes(data, length)))
                process.write_bytes(region + login.FLAG_OFFSET, bytes([login.DONE]), 1)
        await asyncio.sleep(0.001)


# --- the command ---------------------------------------------------------------


def test_the_command_is_the_login_windows_own_null_terminated():
    assert login.login_command("someone", "hunter2!") == b"login someone hunter2!\x00"


@pytest.mark.parametrize(
    "username, password",
    [
        ("", "pw"),
        ("someone", ""),
        ("some one", "pw"),
        ("someone", "pass word"),
        ("someone", "pässword"),
        ("a\tb", "pw"),
    ],
)
def test_a_credential_the_command_cannot_carry_is_refused(username, password):
    with pytest.raises(MemoryLoginError) as raised:
        login.login_command(username, password)
    if password:
        assert password not in str(raised.value)


def test_credentials_too_long_for_the_region_are_refused():
    with pytest.raises(MemoryLoginError):
        login.login_command("u", "p" * login.MAX_COMMAND)


def test_a_short_string_still_says_heap():
    # Under 16, MSVC reads the union as the characters unless the capacity says otherwise
    assert struct.unpack("<QQQQ", login.string_struct(0x1234, 9)) == (0x1234, 0, 9, 16)
    assert struct.unpack("<QQQQ", login.string_struct(0x1234, 40)) == (
        0x1234,
        0,
        40,
        40,
    )


# --- finding the call ----------------------------------------------------------


def test_both_of_the_clients_login_calls_resolve_to_one_client_and_dispatcher():
    # The two copies in the 2026-11-16 client, at their file offsets
    first = login.resolve_login_call(
        0x164B5F8, bytes.fromhex("17 f2 12 02 e8 22 5a da ff")
    )
    second = login.resolve_login_call(
        0x164B95C, bytes.fromhex("b3 ee 12 02 e8 be 56 da ff")
    )
    assert first == second == (0x377A820, 0x13F1030)


# --- the stub ------------------------------------------------------------------


def _stub(**overrides):
    args = {
        "code_address": 0x10_0040,
        "flag_address": 0x10_0000,
        "string_address": 0x10_0010,
        "client_global": 0x5555_0000,
        "dispatch": 0x6666_0000,
        "original": LOAD,
        "return_address": 0x20_0007,
    }
    args.update(overrides)
    return login.build_stub(**args), args


def test_the_stub_skips_the_call_to_the_replayed_instruction_when_not_armed():
    code, _ = _stub()
    jne = code.index(b"\x0f\x85")
    target = jne + 6 + struct.unpack_from("<i", code, jne + 2)[0]
    assert code[target : target + len(LOAD)] == LOAD
    # and it compares the flag against ARMED
    assert b"\x80\x38" + bytes([login.ARMED]) in code[:jne]


def test_the_stub_calls_the_dispatcher_with_the_client_and_string_then_clears_the_flag():
    code, args = _stub()
    client = code.index(b"\x48\xb8" + struct.pack("<Q", args["client_global"]))
    string = code.index(b"\x48\xba" + struct.pack("<Q", args["string_address"]))
    call = code.index(b"\x48\xb8" + struct.pack("<Q", args["dispatch"]) + b"\xff\xd0")
    clear = code.index(b"\xc6\x00" + bytes([login.DONE]))
    assert string < call and client < call < clear
    assert code[client + 10 : client + 13] == b"\x48\x8b\x08"  # mov rcx, [rax]


def test_the_stub_keeps_the_stack_as_aligned_as_it_found_it():
    code, _ = _stub()
    pushes = code.index(b"\x48\x83\xec\x28")
    saved = code[code.index(b"\x50\x51\x52") : pushes]
    # Seven eight-byte pushes and 0x28 of shadow space: 96, a multiple of 16
    assert saved == b"\x50\x51\x52\x41\x50\x41\x51\x41\x52\x41\x53"
    assert (7 * 8 + 0x28) % 16 == 0


def test_the_stub_jumps_back_after_the_site():
    code, args = _stub()
    assert code[-5] == 0xE9
    end = args["code_address"] + len(code)
    assert (
        end + struct.unpack_from("<i", code, len(code) - 4)[0] == args["return_address"]
    )


def test_the_site_jump_reaches_the_stub_and_covers_the_whole_load():
    jump = login.site_jump(0x20_0000, 0x10_0040)
    assert len(jump) == len(LOAD) == login.SITE_LENGTH
    assert 0x20_0005 + struct.unpack_from("<i", jump, 1)[0] == 0x10_0040
    assert jump[5:] == b"\x90\x90"


def test_a_stub_out_of_jump_range_is_refused():
    with pytest.raises(MemoryLoginError):
        login.site_jump(0x7FFF_0000_0000, 0x1000)


# --- sending it ----------------------------------------------------------------


async def test_the_login_runs_once_and_the_site_is_put_back(module):
    seen = []
    frames = asyncio.create_task(_frames(module, seen))
    try:
        await login.login_through(
            MemoryReader(module), "someone", "hunter2!", allocate=_allocate
        )
    finally:
        frames.cancel()
    [(region, command)] = seen
    assert command == b"login someone hunter2!"
    assert module.read_bytes(SITE, len(SITE_BYTES)) == SITE_BYTES
    assert module.freed == [region]
    # nothing of the password is left behind in the freed region
    assert b"hunter2" not in module.read_bytes(region, login.REGION_SIZE)


async def test_a_login_the_game_never_runs_times_out_and_puts_the_site_back(module):
    with pytest.raises(MemoryLoginError, match="did not run"):
        await login.login_through(
            MemoryReader(module),
            "someone",
            "hunter2!",
            timeout=0.05,
            allocate=_allocate,
        )
    assert module.read_bytes(SITE, len(SITE_BYTES)) == SITE_BYTES
    [region] = module.freed
    assert module.read_bytes(region + login.FLAG_OFFSET, 1)[0] != login.ARMED
    assert b"hunter2" not in module.read_bytes(region, login.REGION_SIZE)


async def test_a_site_hooked_already_is_left_alone(module):
    hooked = login.site_jump(SITE, MODULE_BASE + 0xA00)
    module.write_bytes(SITE, hooked, len(hooked))
    allocations = []
    with pytest.raises(MemoryLoginError, match="hooked already"):
        await login.login_through(
            MemoryReader(module),
            "someone",
            "pw",
            allocate=lambda *a: allocations.append(a) or _allocate(*a),
        )
    assert allocations == []
    assert module.read_bytes(SITE, len(hooked)) == hooked


async def test_login_calls_that_disagree_are_refused(module):
    at = MODULE_BASE + 0x200
    module.write_bytes(at, _login_call(at, dispatch=DISPATCH + 0x10), 22)
    with pytest.raises(MemoryLoginError, match="disagree"):
        await login.login_through(
            MemoryReader(module), "someone", "pw", allocate=_allocate
        )


async def test_a_client_without_the_login_call_is_refused(module):
    for at in (MODULE_BASE + 0x100, MODULE_BASE + 0x200):
        module.write_bytes(at, b"\xcc" * 22, 22)
    with pytest.raises(MemoryLoginError, match="not found"):
        await login.login_through(
            MemoryReader(module), "someone", "pw", allocate=_allocate
        )


async def test_memory_login_refuses_a_credential_before_touching_the_window(
    monkeypatch,
):
    looked_up = []
    monkeypatch.setattr(
        login.utils, "get_pid_from_handle", lambda handle: looked_up.append(handle) or 1
    )
    with pytest.raises(MemoryLoginError):
        await login.memory_login(123, "someone", "pass word")
    assert looked_up == []


async def test_memory_login_refuses_a_window_with_no_process(monkeypatch):
    monkeypatch.setattr(login.utils, "get_pid_from_handle", lambda handle: 0)
    with pytest.raises(MemoryLoginError, match="no process"):
        await login.memory_login(123, "someone", "pw")
