import struct
from typing import NamedTuple

from wizwalker.constants import Primitive
from wizwalker.errors import VideoManagerError
from wizwalker.memory.memory_object import MemoryObject


_EXE_NAME = "WizardGraphicalClient.exe"

# The base class's constructor stores `this` in a static slot, and its destructor clears
# it, so the slot holds the one video manager for as long as the game has one. The game
# reads the slot about 4,500 times, so the pattern is over the constructor's store.
_SLOT_PATTERN = (
    rb"\x48\x89\x3D...."  # mov [rip + slot], rdi
    rb"\x48\x8D\x05...."  # lea rax, [rip + vtable]
    rb"\x48\x89\x07"  # mov [rdi], rax
    rb"\x48\x8D\x05...."  # lea rax, [rip + vtable]
    rb"\x48\x89\x47\x08"  # mov [rdi + 8], rax
)
_SLOT_DISPLACEMENT_AT = 3
_SLOT_INSTRUCTION_LENGTH = 7

# GamebryoClient::VidRestart, which applies a pending mode change: the frame function calls
# it while the byte is set. It says where each field below is, so a patch that moves one
# moves what is read here with it. The pattern starts after the prologue, which each
# recompile has changed: r812087 and r813697 open the function differently, and from
# `mov rbx, rcx` on their code is the same, distances and all. r806919 and r812087 also
# inlined it into the frame function, where the old pattern was; r813697 does not.
_FRAME_PATTERN = (
    rb"\x48\x8B\xD9"  # mov rbx, rcx
    rb"\x48\x8B\x89...."  # mov rcx, [rcx + device]
    rb"\xE8...."  # call
    rb"\x85\xC0\x74."  # test eax, eax; je
)
_DEVICE_LOAD_AT = 0x3  # mov rcx, [rcx + device]
_DEVICE_RELOAD_AT = 0xA5  # mov rax, [rbx + device]; test rax, rax; je
_DEVICE_SIZE_AT = 0xB1  # mov edi, [rax + width]; mov esi, [rax + height]
_PENDING_CLEAR_AT = 0x100  # mov byte ptr [rbx + pending], 0
_FRAME_CODE_LENGTH = 0x107

# What a render device's side can be: Direct3D 9's largest texture, and well under any
# size the game offers. Memory that is not a device's size is rarely both.
_DEVICE_SIDES = range(100, 16385)

# A config node's +0x18 points at its value, a std::string.
_NODE_STRING = 0x18


class _FrameFields(NamedTuple):
    pending: int
    device: int
    device_width: int
    device_height: int


def _read_frame_fields(code: bytes) -> _FrameFields:
    """Where VidRestart's code says each field is; raises if it isn't the code expected."""

    def expect(at: int, opcode: bytes, length: int, trailer: bytes = b""):
        found = code[at : at + length]
        if found[: len(opcode)] != opcode or (trailer and found[-len(trailer) :] != trailer):
            raise VideoManagerError(f"Expected {opcode.hex()} at match + {at:#x}, read {found.hex()}")
        return found

    def rax_dword(at: int) -> tuple[int, int]:
        # mov r32, [rax + disp8], any destination: (the displacement, the instruction's length).
        # A recompile picks the registers, and r813697 picked others than r806919.
        prefix = 1 if code[at : at + 1] == b"\x44" else 0
        found = code[at : at + prefix + 3]
        if len(found) < prefix + 3 or found[prefix] != 0x8B or found[prefix + 1] & 0xC7 != 0x40:
            raise VideoManagerError(f"Expected mov r32, [rax + disp8] at match + {at:#x}, read {found.hex()}")
        return found[prefix + 2], prefix + 3

    pending = struct.unpack_from("<I", expect(_PENDING_CLEAR_AT, b"\xC6\x83", 7, b"\x00"), 2)[0]

    # The device is read twice, once to test it and once for its size, and both must agree.
    # The pattern holds the first load.
    device = struct.unpack_from("<I", code, _DEVICE_LOAD_AT + 3)[0]
    reloaded = expect(_DEVICE_RELOAD_AT, b"\x48\x8B\x83", 12)
    if reloaded[7:11] != b"\x48\x85\xC0\x74":
        raise VideoManagerError(
            f"Expected a test of the device at match + {_DEVICE_RELOAD_AT + 7:#x}, read {reloaded.hex()}"
        )
    if device != struct.unpack_from("<I", reloaded, 3)[0]:
        raise VideoManagerError(f"The device is loaded from {device:#x} and from {reloaded[3:7].hex()}")

    width, length = rax_dword(_DEVICE_SIZE_AT)
    height, _ = rax_dword(_DEVICE_SIZE_AT + length)

    return _FrameFields(pending, device, width, height)


class VideoManager(MemoryObject):
    """
    The game's video manager, which changes a running client's resolution

    Patches no game code: the manager comes from the static slot and its fields' offsets
    from VidRestart's code, so a resize has nothing to install or remove. Code is
    static for the life of the process, so each scan runs once. Every way this can fail
    raises before anything is written.

    Raises:
        VideoManagerError: The code or the manager is not what this expects
        MemoryReadError: What the slot holds does not lead to a device
        PatternFailed: A pattern has no match, on a build it no longer fits
        PatternMultipleResults: A pattern has more than one match
    """

    def __init__(self, hook_handler):
        super().__init__(hook_handler)
        self._slot = None
        self._frame_fields = None
        self._checked_manager = None

    async def _read_slot(self) -> int:
        if self._slot is None:
            match = await self.pattern_scan(_SLOT_PATTERN, module=_EXE_NAME)
            displacement = await self.read_typed(match + _SLOT_DISPLACEMENT_AT, Primitive.int32)
            self._slot = match + _SLOT_INSTRUCTION_LENGTH + displacement
        return self._slot

    async def _read_fields(self) -> _FrameFields:
        if self._frame_fields is None:
            match = await self.pattern_scan(_FRAME_PATTERN, module=_EXE_NAME)
            self._frame_fields = _read_frame_fields(await self.read_bytes(match, _FRAME_CODE_LENGTH))
        return self._frame_fields

    async def _device_size_of(self, manager: int) -> tuple[int, int]:
        fields = await self._read_fields()
        device = await self.read_typed(manager + fields.device, Primitive.int64)
        if not device:
            raise VideoManagerError(f"The object at {manager:#x} has no device")
        return (
            await self.read_typed(device + fields.device_width, Primitive.uint32),
            await self.read_typed(device + fields.device_height, Primitive.uint32),
        )

    async def read_base_address(self) -> int:
        """
        The video manager, once what it holds has been seen to be a render device's size

        A write of the pending byte lands wherever this says, with no error if it is wrong.
        Its vtable can't vouch for it: the game's class overrides the frame function and
        calls it directly, so the function's address is in no table this object uses.
        """
        manager = await self.read_typed(await self._read_slot(), Primitive.int64)
        if not manager:
            raise VideoManagerError("The video manager's slot is empty")

        if manager != self._checked_manager:
            width, height = await self._device_size_of(manager)
            if width not in _DEVICE_SIDES or height not in _DEVICE_SIDES:
                raise VideoManagerError(
                    f"The object at {manager:#x} has a device of size {width}x{height}"
                )
            self._checked_manager = manager

        return manager

    async def pending_flag_offset(self) -> int:
        """
        Where the pending byte is on the manager, read from the code that clears it

        A patch moves it, and the old offset is some other byte. r813697 tests it in the
        frame function and no longer in VidRestart, so the clear is the one site the match
        reaches; the device's two loads are what must agree.
        """
        return (await self._read_fields()).pending

    async def read_device_size(self) -> tuple[int, int]:
        """
        The render device's width and height, the size the game is drawing at
        """
        return await self._device_size_of(await self.read_base_address())

    async def apply_resolution(self, resolution_node: int, width: int, height: int):
        """
        Have the game change to width x height on its next frame, as its own options menu does

        It takes the size from the node, then moves its own device, window and camera, and
        clears the byte when done: apply_pending says when.

        Args:
            resolution_node: The address of the live Resolution config node
            width: The width to change to
            height: The height to change to
        """
        manager = await self.read_base_address()
        pending_offset = await self.pending_flag_offset()

        string = await self.read_typed(resolution_node + _NODE_STRING, Primitive.int64)
        if not string:
            raise VideoManagerError(f"The node at {resolution_node:#x} holds no string")

        await self.write_string(string, f"{width}x{height}")
        await self.write_typed(manager + pending_offset, True, Primitive.bool)

    async def apply_pending(self) -> bool:
        """
        If a change asked for has yet to finish

        The game clears the byte after the device reset, and a failed attempt leaves it set
        to try again on the next frame.
        """
        return await self.read_value_from_offset(await self.pending_flag_offset(), Primitive.bool)
