import struct
from types import SimpleNamespace

import pytest
import regex

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

# The "Loot" event sender's opening as r811339 compiles it, with made-up operands
_LOOT_SENDER = bytes.fromhex(
    "48 83 ec 68"  # sub rsp, 0x68 (the jump overwrites this...)
    "48 c7 44 24 30 fe ff ff ff"  # mov qword [rsp+0x30], -2 (...and this)
    "48 8b 05 11 11 11 11 48 33 c4 48 89 44 24 58"  # the stack cookie
    "48 8d 05 22 22 22 22 48 89 44 24 20 48 89 54 24 28"  # the handler, and rdx
    "66 0f 6f 05 33 33 33 33 f3 0f 7f 44 24 48 c6 44 24 38 00"  # an empty string
    "41 b8 04 00 00 00 48 8d 15 44 44 44 44"  # assigned "Loot", four characters
)


def test_the_drops_hook_finds_the_loot_sender():
    page = b"\xcc" * 16 + _LOOT_SENDER + b"\xcc" * 16
    assert [m.start() for m in regex.finditer(hooks.DropsToggleHook.pattern, page, regex.DOTALL)] == [16]


async def test_the_drops_hook_replays_what_its_jump_overwrites(process, hook_handler):
    hook = hooks.DropsToggleHook(hook_handler)
    hook.jump_address = process.map(0x7000, _LOOT_SENDER)
    code = await hook.bytecode_generator([("disable_drops_bool", struct.pack("<Q", 0x1234))])
    assert code.endswith(_LOOT_SENDER[:13])
    # Disabled, it returns before them: `xor rax, rax; ret` comes first
    assert code.index(b"\x48\x31\xc0\xc3") < len(code) - 13


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


async def test_the_mouseless_hook_answers_get_cursor_pos_as_it_would(process, hook_handler):
    code = await hooks.MouselessCursorMoveHook(hook_handler).get_hook_bytecode()
    # TRUE in eax, then ret: the caller reads eax as success
    assert code.endswith(b"\xB8\x01\x00\x00\x00\xC3")
