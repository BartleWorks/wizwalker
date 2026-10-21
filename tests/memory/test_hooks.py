import struct
from types import SimpleNamespace

import pytest

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


# --- movement teleport ---------------------------------------------------------


@pytest.mark.parametrize("helper", [0x7F00_0001_0000, 0x7F00_0001_00F8], ids=["aligned", "low byte near 0xFF"])
async def test_teleport_bytecode_addresses_the_helpers_fields(process, hook_handler, helper):
    # Each field's address was the helper's with only its low byte raised: past
    # 0xFF that raised ValueError, and the carry was never taken.
    jes = (process.map(0x2000, b"\x0F\x84" + b"\x11" * 6), process.map(0x3000, b"\x0F\x85" + b"\x22" * 6))

    async def get_jes():
        return jes

    hook_handler.client = SimpleNamespace(_get_je_instruction_forward_backwards=get_jes)
    hook = hooks.MovementTeleportHook(hook_handler)
    code = await hook.bytecode_generator([("teleport_helper", struct.pack("<Q", helper))])

    for field in (8, 12, 13):
        assert struct.pack("<Q", helper + field) in code
    assert hook._old_jes_bytes == (process.read_bytes(jes[0], 8), process.read_bytes(jes[1], 8))


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
