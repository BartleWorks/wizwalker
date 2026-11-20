import asyncio
import ctypes
import os
import struct
from types import SimpleNamespace

import pymem.exception
import pymem.process
import pytest
import regex

from wizwalker import PatternFailed
from wizwalker.errors import MemoryWriteError
from wizwalker.memory import hooks
from wizwalker.memory.handler import HookHandler

JUMP = 0x1400_0000
ORIGINAL = bytes(range(0x40, 0x60))


def decode_jmp(data: bytes, at: int) -> int:
    """Where an E9 rel32 jmp written at `at` lands."""
    assert data[0] == 0xE9
    return at + 5 + struct.unpack("<i", data[1:5])[0]


@pytest.fixture
def autobot(process, hook_handler):
    # Inside the game's own module, as the real one is, so a rel32 jmp reaches it
    hook_handler._autobot_address = process.map(JUMP + 0x10_0000, size=HookHandler.AUTOBOT_SIZE)
    return hook_handler


@pytest.fixture
def jump_site(process, monkeypatch):
    process.map(JUMP, ORIGINAL)

    async def scan(self, pattern, *, module=None, return_multiple=False):
        # ClientHook's pattern starts a byte before the instruction it moves
        return JUMP - 1 if isinstance(self, hooks.ClientHook) else JUMP

    monkeypatch.setattr(hooks.MemoryReader, "pattern_scan", scan)
    return JUMP


@pytest.mark.parametrize("method", ["get_jump_bytecode", "get_hook_bytecode", "get_pattern"])
async def test_an_unfinished_hook_raises_not_implemented(hook_handler, method):
    # `raise NotImplemented()` was a TypeError: NotImplemented is not callable
    with pytest.raises(NotImplementedError):
        await getattr(hooks.MemoryHook(hook_handler), method)()


async def test_a_simple_hook_without_bytecode_raises_not_implemented(hook_handler):
    with pytest.raises(NotImplementedError):
        await hooks.SimpleHook(hook_handler).bytecode_generator([])


SIMPLE_HOOKS = [
    hooks.PlayerHook,
    hooks.PlayerStatHook,
    hooks.QuestHook,
    hooks.ClientHook,
    hooks.RootWindowHook,
    hooks.RenderContextHook,
    hooks.DropsToggleHook,
]


@pytest.mark.parametrize("hook_type", SIMPLE_HOOKS, ids=lambda t: t.__name__)
async def test_a_simple_hook_jumps_out_and_back(process, autobot, jump_site, hook_type):
    hook = hook_type(autobot)
    await hook.hook()

    jump = process.read_bytes(JUMP, len(hook.jump_bytecode))
    assert decode_jmp(jump, JUMP) == hook.hook_address
    # The jmp's spare bytes are nops, so returning past the moved instruction
    # into them is harmless
    assert jump[5:] == b"\x90" * hook.noops
    assert len(jump) >= hook.instruction_length

    written = process.read_bytes(hook.hook_address, len(hook.hook_bytecode))
    back = len(written) - 5
    assert decode_jmp(written[back:], hook.hook_address + back) == JUMP + hook.instruction_length

    for name, _size in hook.exports:
        export = getattr(hook, name)
        assert struct.pack("<Q", export) in written


@pytest.mark.parametrize("hook_type", SIMPLE_HOOKS, ids=lambda t: t.__name__)
async def test_unhooking_restores_the_site_and_frees_the_exports(process, autobot, jump_site, hook_type):
    hook = hook_type(autobot)
    await hook.hook()
    await hook.unhook()

    assert process.read_bytes(JUMP, len(ORIGINAL)) == ORIGINAL
    for name, _size in hook.exports:
        assert getattr(hook, name) in process.freed


async def test_the_autobot_refuses_more_than_it_holds(autobot):
    autobot._autobot_pos = HookHandler.AUTOBOT_SIZE - 10
    with pytest.raises(RuntimeError):
        await autobot._allocate_autobot_bytes(50)


# --- drops ---------------------------------------------------------------------

# The "Loot" event sender's opening as each compiler builds it, with made-up operands.
# The old compiler (r806919, r812087, r814353):
_OLD_LOOT_SENDER = bytes.fromhex(
    "4c 8b dc"  # mov r11, rsp (the jump overwrites this...)
    "48 83 ec 68"  # sub rsp, 0x68 (...and this)
    "48 8b 05 11 11 11 11 48 33 c4 48 89 44 24 58"  # the stack cookie
    "48 8d 05 22 22 22 22"  # the handler
)
# The new compiler (r811339, r813697):
_NEW_LOOT_SENDER = bytes.fromhex(
    "48 83 ec 68"  # sub rsp, 0x68 (the jump overwrites this...)
    "48 c7 44 24 30 fe ff ff ff"  # mov qword [rsp+0x30], -2 (...and this)
    "48 8b 05 11 11 11 11 48 33 c4 48 89 44 24 58"  # the stack cookie
    "48 8d 05 22 22 22 22 48 89 44 24 20 48 89 54 24 28"  # the handler, and rdx
    "66 0f 6f 05 33 33 33 33 f3 0f 7f 44 24 48 c6 44 24 38 00"  # an empty string
    "41 b8 04 00 00 00 48 8d 15 44 44 44 44"  # assigned "Loot", four characters
)
_OLD_DROPS, _NEW_DROPS = hooks.DropsToggleHook.sites
_LOOT_SENDERS = [(_OLD_LOOT_SENDER, _OLD_DROPS), (_NEW_LOOT_SENDER, _NEW_DROPS)]


@pytest.fixture
def loaded_function(process, monkeypatch):
    """Lays one build's function out at JUMP, and scans for patterns over it as the client would."""

    def install(function: bytes):
        process.map(JUMP, function)

        async def scan(self, pattern, *, module=None, return_multiple=False):
            found = _scan(pattern, function)
            if not found:
                raise PatternFailed(pattern)
            return JUMP + found[0]

        monkeypatch.setattr(hooks.MemoryReader, "pattern_scan", scan)

    return install


@pytest.mark.parametrize("function, site", _LOOT_SENDERS, ids=["old", "new"])
def test_each_drops_site_finds_its_builds_loot_sender_and_not_the_others(function, site):
    other = _NEW_LOOT_SENDER if function is _OLD_LOOT_SENDER else _OLD_LOOT_SENDER
    page = b"\xcc" * 16 + function + b"\xcc" * 16
    assert _scan(site.pattern, page) == [16]
    assert _scan(site.pattern, b"\xcc" * 16 + other + b"\xcc" * 16) == []


@pytest.mark.parametrize("function, site", _LOOT_SENDERS, ids=["old", "new"])
async def test_the_drops_hook_takes_its_builds_site_and_replays_what_it_overwrites(
    process, autobot, loaded_function, function, site
):
    loaded_function(function)
    hook = hooks.DropsToggleHook(autobot)
    await hook.hook()

    assert (hook.instruction_length, hook.noops) == (site.instruction_length, site.noops)
    # The jump and its nops cover the instructions it overwrites, and no more
    assert len(hook.jump_bytecode) == site.instruction_length
    # Enabled, the cave runs those instructions and jumps back after them
    code = hook.hook_bytecode
    assert code[:-5].endswith(function[: site.instruction_length])
    assert decode_jmp(code[-5:], hook.hook_address + len(code) - 5) == JUMP + site.instruction_length
    # Disabled, it returns before them: `xor rax, rax; ret` comes first
    assert code.index(b"\x48\x31\xc0\xc3") < len(code) - 5 - site.instruction_length


async def test_a_hook_with_sites_raises_when_none_of_them_matches(autobot, loaded_function):
    loaded_function(b"\xcc" * 64)
    with pytest.raises(PatternFailed):
        await hooks.DropsToggleHook(autobot).hook()


# --- the root window -----------------------------------------------------------


def _root_window_site(load: str) -> bytes:
    """The root window's load and the vtable call after it, with made-up operands."""
    return bytes.fromhex(load + "48 8b 01" + "49 8b 97 00 01 00 00" + "ff 50 70 84 db")


@pytest.mark.parametrize("load", ["49 8b 8d 00 02 00 00", "49 8b 8f 00 02 00 00"], ids=["r13", "r15"])
def test_the_root_window_hook_takes_the_load_into_rcx_from_any_register(load):
    page = b"\xcc" * 16 + _root_window_site(load)
    assert [m.start() for m in regex.finditer(hooks.RootWindowHook.pattern, page, regex.DOTALL)] == [16]


def test_the_root_window_hook_refuses_a_load_that_is_not_seven_bytes():
    # ModRM 0x8C takes a SIB byte: [r12+disp32] is eight bytes, not the seven replayed
    page = b"\xcc" * 16 + _root_window_site("49 8b 8c 24 00 02 00 00")
    assert not regex.search(hooks.RootWindowHook.pattern, page, regex.DOTALL)


async def test_the_root_window_hook_exports_rcx_after_replaying_the_load(process, hook_handler):
    load = bytes.fromhex("49 8b 8f 00 02 00 00")
    hook = hooks.RootWindowHook(hook_handler)
    hook.jump_address = process.map(0x8000, _root_window_site(load.hex(" ")))
    code = await hook.bytecode_generator([("current_root_window_addr", struct.pack("<Q", 0x1234))])
    assert code.startswith(load)
    assert code[len(load) :].startswith(b"\x50\x48\x89\xc8\x48\xa3" + struct.pack("<Q", 0x1234))


# --- the client hook's site ----------------------------------------------------


def _client_site(slot: int) -> bytes:
    """ClientHook's site with made-up operands: the instruction forms are the client's."""
    return bytes.fromhex(
        "48 8b 18"  # mov rbx, [rax]
        "48 8b 9b 00 02 00 00"  # mov rbx, [rbx+0x200]: the instruction the hook moves
        f"48 8b 7c 24 {slot:02x}"  # mov rdi, [rsp+slot]
        "48 85 ff 74 29 8b c6 f0 0f c1 47 08 83 f8 01 75 1d"  # test; je; lock xadd [rdi+8]
        "48 8b 07 48 8b cf ff 50 08 f0 0f c1 77 0c"  # the release call; lock xadd [rdi+0xC]
    )


@pytest.mark.parametrize("slot", [0x38, 0x40], ids=["r806919", "r811339"])
def test_the_client_hook_finds_its_site_wherever_the_stack_slot_is(slot):
    page = b"\xcc" * 16 + _client_site(slot) + b"\xcc" * 16
    matches = [m.start() for m in regex.finditer(hooks.ClientHook.pattern, page, regex.DOTALL)]
    # get_jump_address adds the one byte the pattern starts early
    assert [m + 1 for m in matches] == [16 + 3]


# --- movement teleport ---------------------------------------------------------

# The set-position function from its hook site on, with made-up operands
_TELEPORT_SITE = bytes.fromhex(
    "48 8b 99 00 02 00 00"  # mov rbx, [rcx+0x200]: the instruction the hook moves
    "48 85 db 74 2f 4c 8b 43 70 48 8b 5b 78 48 85 db 74 04 f0 ff 43 08"
)


@pytest.mark.parametrize("helper", [0x7F00_0001_0000, 0x7F00_0001_00F8], ids=["aligned", "low byte near 0xFF"])
async def test_teleport_bytecode_addresses_the_helpers_fields(process, hook_handler, helper):
    # Each field's address was the helper's with only its low byte raised: past
    # 0xFF that raised ValueError, and the carry was never taken.
    jes = (process.map(0x2000, b"\x0F\x84" + b"\x11" * 6), process.map(0x3000, b"\x0F\x85" + b"\x22" * 6))

    async def get_jes():
        return jes

    hook_handler.client = SimpleNamespace(_get_je_instruction_forward_backwards=get_jes)
    hook = hooks.MovementTeleportHook(hook_handler)
    hook.jump_address = process.map(0x4000, _TELEPORT_SITE)
    code = await hook.bytecode_generator([("teleport_helper", struct.pack("<Q", helper))])

    for field in (8, 12, 13):
        assert struct.pack("<Q", helper + field) in code
    assert hook._old_jes_bytes == (process.read_bytes(jes[0], 8), process.read_bytes(jes[1], 8))
    # The instruction the jump overwrote is replayed last, read from the site
    assert code.endswith(_TELEPORT_SITE[: hook.instruction_length])


def _scan(pattern: bytes, page: bytes) -> list[int]:
    return [m.start() for m in regex.finditer(pattern, page, regex.DOTALL)]


def test_the_teleport_hook_lands_on_the_first_instruction_after_either_prologue():
    for prologue in (
        bytes.fromhex("48 89 5c 24 08 57 48 83 ec 20"),  # r806919
        bytes.fromhex("40 57 48 83 ec 30 48 c7 44 24 20 fe ff ff ff 48 89 5c 24 40"),  # r811339
    ):
        assert _scan(hooks.MovementTeleportHook.pattern, prologue + _TELEPORT_SITE) == [len(prologue)]


def _collision_block(r8_setup: bytes, dispatch_slot: int) -> bytes:
    """One of the movement function's two collision blocks, with made-up operands."""
    return (
        r8_setup
        + bytes.fromhex("48 8d 55 a0 ff 50 10 84 c0 74 7a")  # lea rdx; call [rax+0x10]; test; je
        + bytes.fromhex("f3 0f 10 55 a8 f3 0f 58 54 24 48") * 4  # adding the offset in
        + bytes.fromhex(f"4c 8d 44 24 {dispatch_slot:02x} 48 8d 54 24 60 ff 50 18 84 c0 74 24")  # the dispatch
    )


_BLOCK_A = _collision_block(bytes.fromhex("4c 8d 44 24 40"), 0x58)  # lea r8, [rsp+0x40]
_BLOCK_B = _collision_block(bytes.fromhex("4d 8b c7"), 0x54)  # mov r8, r15


@pytest.mark.parametrize("blocks", [(_BLOCK_A, _BLOCK_B), (_BLOCK_B, _BLOCK_A)], ids=["A first", "B first"])
async def test_the_collision_jes_are_the_stack_buffer_blocks_check_and_its_dispatch(process, hook_handler, blocks):
    base = 0x5000
    # Padded past the read-ahead, as the rest of the function would be
    page = b"\xcc" * 8 + b"".join(blocks) + b"\xcc" * hooks.MovementTeleportHook.EVENT_DISPATCH_WITHIN
    process.map(base, page)
    a_at = 8 + (0 if blocks[0] is _BLOCK_A else len(_BLOCK_B))

    [inside] = _scan(hooks.MovementTeleportHook.INSIDE_EVENT_JE_PATTERN, page)
    assert inside == a_at + 14  # the je, past the lea r8 and the call

    hook = hooks.MovementTeleportHook(hook_handler)
    dispatch = await hook._event_dispatch_je_after(base + inside)
    # Block A's own dispatch, wherever block B is and whichever stack slot it names
    assert dispatch - base == a_at + len(_BLOCK_A) - 2
    assert page[dispatch - base] == 0x74


# --- chat send -----------------------------------------------------------------


def test_an_action_block_skips_its_body_when_untriggered(hook_handler):
    hook = hooks.ChatSendHook(hook_handler)
    trigger = struct.pack("<Q", 0x7F00_0000_1234)
    body, clear = hook._save_restore_regs(b"\xFF\xD0", trigger)
    block = hook._build_action_block(trigger, body, b"", clear)

    check_length = 19  # push, mov al, test, pop, then a six byte jz
    assert block[13:15] == b"\x0F\x84"
    assert struct.unpack("<i", block[15:19])[0] == len(block) - check_length
    assert block.endswith(clear)


# --- user32's spare bytes, for the mouseless hook --------------------------------


@pytest.fixture
def class_info(process, monkeypatch):
    address = process.map(0x5000_0000, bytes(range(256)))

    async def symbol(self, module_name, symbol_name, **kwargs):
        return address

    monkeypatch.setattr(hooks.MemoryReader, "get_address_from_symbol", symbol)
    return address


async def test_the_user32_region_refuses_more_than_it_holds(hook_handler, class_info):
    hook = hooks.User32GetClassInfoBaseHook(hook_handler)
    assert await hook.alloc(50) == class_info
    with pytest.raises(RuntimeError):
        await hook.alloc(hooks.User32GetClassInfoBaseHook.AUTOBOT_SIZE)


async def test_the_user32_region_is_put_back_as_far_as_it_was_used(process, hook_handler, class_info):
    hook = hooks.User32GetClassInfoBaseHook(hook_handler)
    used = await hook.alloc(50)
    process.write_bytes(used, b"\xCC" * 50, 50)
    hook.jump_address = process.map(0x6000_0000, b"jmp!!")
    hook.jump_original_bytecode = b"orig!"
    hook._hooked_instances = 1

    await hook.unhook()

    assert process.read_bytes(class_info, 256) == bytes(range(256))
    assert process.read_bytes(hook.jump_address, 5) == b"orig!"


# The window input handler's clear of its mouse-over flag on WM_MOUSELEAVE, each
# build's way, with the clear of the mouse-look flag beside it that a loose pattern finds
_OLD_MOUSE_OVER_CLEAR = bytes.fromhex("c6 86 90 01 00 00 00 33 ff 89 7d 70")  # [rsi+0x190]; xor edi
_NEW_MOUSE_OVER_CLEAR = bytes.fromhex("41 c6 86 90 01 00 00 00 33 f6 89 b5 70 01 00 00")  # [r14+0x190]; xor esi
_MOUSE_LOOK_CLEAR = bytes.fromhex("c6 86 34 01 00 00 00 33 ff 48 89 7c 24 70")  # [rsi+0x134]; xor edi


@pytest.mark.parametrize(
    "clear, immediate_at",
    [(_OLD_MOUSE_OVER_CLEAR, 6), (_NEW_MOUSE_OVER_CLEAR, 7)],
    ids=["r806919 and r813697", "r812087 and r814353"],
)
async def test_the_mouseless_hook_finds_the_mouse_over_clears_immediate(
    process, hook_handler, loaded_function, clear, immediate_at
):
    # The mouse-look clear comes first, as a scan from the module's start would meet it
    loaded_function(_MOUSE_LOOK_CLEAR + b"\xcc" * 4 + clear)
    found = await hooks.MouselessCursorMoveHook(hook_handler)._find_mouse_over_clear()

    assert found == JUMP + len(_MOUSE_LOOK_CLEAR) + 4 + immediate_at
    # The byte it will overwrite is the clear's 0, the last byte of `mov byte [reg+0x190], 0`
    assert process.read_bytes(found - 4, 5) == bytes.fromhex("90 01 00 00 00")


async def test_the_mouseless_hook_does_not_settle_for_the_mouse_look_clear(hook_handler, loaded_function):
    loaded_function(_MOUSE_LOOK_CLEAR)
    with pytest.raises(PatternFailed):
        await hooks.MouselessCursorMoveHook(hook_handler)._find_mouse_over_clear()


async def test_the_mouseless_hook_answers_get_cursor_pos_as_it_would(process, hook_handler):
    code = await hooks.MouselessCursorMoveHook(hook_handler).get_hook_bytecode()
    # TRUE in eax, then ret: the caller reads eax as success
    assert code.endswith(b"\xB8\x01\x00\x00\x00\xC3")


# --- the mouseless hook's writes, and only its own bytes taken for the originals --


GET_CURSOR_POS = 0x7000_0000
SET_CURSOR_POS = 0x7000_1000
CLASS_INFO = 0x7000_2000
_GET_CURSOR_POS_CODE = bytes.fromhex("ba 01 00 00 00 48 ff 25 bc 8b 07 00 cc cc cc cc")
_SET_CURSOR_POS_CODE = bytes.fromhex("ff 25 62 0c 05 00 cc cc cc cc cc cc cc cc cc cc")
_TOGGLE_ONE = b"\x00\xFF\x50\x18\x66\xC7"
# As the hook scans for it: a regex, its escapes written out
_TOGGLE_ONE_PATTERN = rb"\x00\xFF\x50\x18\x66\xC7"


class CursorGame:
    """user32's three functions the mouseless hook writes into, and the client code holding its two toggles."""

    def __init__(self, process, hook_handler, toggle_one: int, mouse_over: int):
        self.process = process
        self.hook_handler = hook_handler
        self.toggle_one = toggle_one
        self.mouse_over = mouse_over
        # What our own user32 holds at an address, standing in for _clean_user32_bytes
        self.clean: dict[int, bytes] | None = None
        # Which lookup waits until it is cancelled: a symbol's name, or "the toggle" for its scan
        self.stall_at: str | None = None
        self.stalled = asyncio.Event()

    async def stall(self):
        self.stalled.set()
        await asyncio.Event().wait()

    def snapshot(self) -> dict[str, bytes]:
        read = self.process.read_bytes
        return {
            "GetCursorPos": read(GET_CURSOR_POS, 16),
            "SetCursorPos": read(SET_CURSOR_POS, 16),
            "GetClassInfoExA": read(CLASS_INFO, 256),
            "toggles": read(self.toggle_one, 1) + read(self.mouse_over, 1),
        }

    def hook(self) -> hooks.MouselessCursorMoveHook:
        return hooks.MouselessCursorMoveHook(self.hook_handler)


@pytest.fixture
def cursor_game(process, hook_handler, loaded_function, monkeypatch) -> CursorGame:
    process.map(GET_CURSOR_POS, _GET_CURSOR_POS_CODE)
    process.map(SET_CURSOR_POS, _SET_CURSOR_POS_CODE)
    process.map(CLASS_INFO, bytes(range(256)))
    gap = b"\xcc" * 4
    loaded_function(_TOGGLE_ONE + gap + _NEW_MOUSE_OVER_CLEAR)
    game = CursorGame(process, hook_handler, toggle_one=JUMP, mouse_over=JUMP + len(_TOGGLE_ONE) + len(gap) + 7)

    symbols = {"GetCursorPos": GET_CURSOR_POS, "SetCursorPos": SET_CURSOR_POS, "GetClassInfoExA": CLASS_INFO}
    scan = hooks.MemoryReader.pattern_scan

    async def symbol(self, module_name, symbol_name, **kwargs):
        if symbol_name == game.stall_at:
            await game.stall()
        return symbols[symbol_name]

    async def stalling_scan(self, pattern, **kwargs):
        if game.stall_at == "the toggle" and pattern == _TOGGLE_ONE_PATTERN:
            await game.stall()
        return await scan(self, pattern, **kwargs)

    def clean(process, address, size):
        if game.clean is None:
            return None
        base = max((b for b in game.clean if b <= address), default=None)
        return None if base is None else game.clean[base][address - base : address - base + size]

    monkeypatch.setattr(hooks.MemoryReader, "get_address_from_symbol", symbol)
    monkeypatch.setattr(hooks.MemoryReader, "pattern_scan", stalling_scan)
    # raising=False, so the tests also run against a hook that has no clean copy to read
    monkeypatch.setattr(hooks, "_clean_user32_bytes", clean, raising=False)
    return game


async def test_the_mouseless_hook_writes_its_four_patches_and_unhooking_takes_them_all_out(cursor_game):
    before = cursor_game.snapshot()
    hook = cursor_game.hook()

    await hook.hook()

    hooked = cursor_game.snapshot()
    assert decode_jmp(hooked["GetCursorPos"], GET_CURSOR_POS) == CLASS_INFO
    assert hooked["GetCursorPos"][5:] == before["GetCursorPos"][5:]
    assert hooked["GetClassInfoExA"][:50] != before["GetClassInfoExA"][:50]
    assert hooked["toggles"] == b"\x01\x01"
    assert hooked["SetCursorPos"][:6] == b"\xC3" + b"\x90" * 5

    await hook.unhook()

    assert cursor_game.snapshot() == before


@pytest.mark.parametrize("lookup", ["the toggle", "SetCursorPos"])
async def test_a_mouseless_hook_cancelled_at_a_lookup_has_written_nothing(cursor_game, lookup):
    # Both used to come after the jump was written, so a cancel there left it in the game
    # with nothing recorded to take it out
    before = cursor_game.snapshot()
    cursor_game.stall_at = lookup

    task = asyncio.create_task(cursor_game.hook().hook())
    await cursor_game.stalled.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cursor_game.snapshot() == before


async def test_a_mouseless_hook_whose_last_write_fails_puts_back_what_it_wrote(cursor_game, monkeypatch):
    before = cursor_game.snapshot()
    write = cursor_game.process.write_bytes

    def refuse_the_ret(address, value, size):
        if address == SET_CURSOR_POS and value.startswith(b"\xC3"):
            raise pymem.exception.MemoryWriteError(address, value)
        write(address, value, size)

    monkeypatch.setattr(cursor_game.process, "write_bytes", refuse_the_ret)

    with pytest.raises(MemoryWriteError):
        await cursor_game.hook().hook()

    assert cursor_game.snapshot() == before


async def _leave_a_leftover(game: CursorGame) -> dict[str, bytes]:
    """The game as P2's was found: GetCursorPos jumping to hook code left in GetClassInfoExA, the rest put back."""
    clean = game.snapshot()
    leaked = game.hook()
    await leaked.hook()
    leftover = game.snapshot()
    await leaked.unhook()
    game.process.write_bytes(GET_CURSOR_POS, leftover["GetCursorPos"], 16)
    game.process.write_bytes(CLASS_INFO, leftover["GetClassInfoExA"], 256)
    return clean


async def test_a_hooks_leftovers_are_not_taken_for_the_originals(cursor_game):
    clean = await _leave_a_leftover(cursor_game)
    cursor_game.clean = {
        GET_CURSOR_POS: clean["GetCursorPos"],
        SET_CURSOR_POS: clean["SetCursorPos"],
        CLASS_INFO: clean["GetClassInfoExA"],
    }

    hook = cursor_game.hook()
    await hook.hook()
    await hook.unhook()

    assert cursor_game.snapshot() == clean


async def test_without_a_clean_copy_of_user32_the_games_own_bytes_are_the_originals(cursor_game):
    await _leave_a_leftover(cursor_game)
    left = cursor_game.snapshot()

    hook = cursor_game.hook()
    await hook.hook()
    await hook.unhook()

    assert cursor_game.snapshot() == left


# --- user32 as this process has it ------------------------------------------------


@pytest.fixture
def own_user32():
    """A handle to this process, user32's base in it, and where its GetCursorPos is."""
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    user32 = ctypes.WinDLL("user32")
    get_cursor_pos = ctypes.cast(user32.GetCursorPos, ctypes.c_void_p).value
    handle = pymem.process.open(os.getpid())
    yield handle, kernel32.GetModuleHandleW("user32.dll"), get_cursor_pos
    pymem.process.close_handle(handle)


def test_our_own_user32_is_read_at_the_games_address_while_the_bases_match(own_user32):
    handle, _, get_cursor_pos = own_user32
    found = hooks._clean_user32_bytes(SimpleNamespace(process_handle=handle), get_cursor_pos, 5)
    assert found == ctypes.string_at(get_cursor_pos, 5)


def test_our_own_user32_is_not_read_for_a_game_that_loads_it_elsewhere(own_user32, monkeypatch):
    handle, base, get_cursor_pos = own_user32
    elsewhere = SimpleNamespace(lpBaseOfDll=base + 0x10_0000, SizeOfImage=0x20_0000)
    monkeypatch.setattr(hooks.pymem.process, "module_from_name", lambda *args: elsewhere)
    assert hooks._clean_user32_bytes(SimpleNamespace(process_handle=handle), get_cursor_pos, 5) is None


def test_our_own_user32_is_not_read_outside_it(own_user32):
    handle, base, _ = own_user32
    assert hooks._clean_user32_bytes(SimpleNamespace(process_handle=handle), base - 0x1000, 5) is None
