import struct

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

# The virtual function the game runs each frame, which applies a pending mode change.
_FRAME_PATTERN = (
    rb"\x48\x8B\xC4\x48\x89\x58\x10\x48\x89\x70\x18\x55\x57\x41\x56"
    rb"\x48\x8D\x68\x88\x48\x81\xEC\x60\x01\x00\x00"
    rb"\x0F\x29\x70\xD8\x0F\x29\x78\xC8\x44\x0F\x29\x40\xB8"
    rb"\x48\x8B\x05....\x48\x33\xC4\x48\x89\x45\x20\x48\x8B\xD9"
)
_PENDING_TEST_AT = 0x3D  # cmp byte ptr [rcx + pending], 0
_PENDING_CLEAR_AT = 0x1A0  # mov byte ptr [rbx + pending], 0

# The frame function's entry moves between builds (89 on r806919, 90 on r812087), so the
# table is searched, as far as this.
_VTABLE_ENTRIES = 256

# A config node's +0x18 points at its value, a std::string.
_NODE_STRING = 0x18


def _displacement(code: bytes, opcode: bytes, at: int) -> int:
    """The disp32 of `opcode disp32 00` at match + `at`; raises if the code is anything else."""
    if code[:2] != opcode or code[6] != 0:
        raise VideoManagerError(f"Expected {opcode.hex()} at match + {at:#x}, read {code.hex()}")
    return struct.unpack_from("<I", code, 2)[0]


class VideoManager(MemoryObject):
    """
    The game's video manager, which changes a running client's resolution

    Patches no game code: the manager comes from the static slot and the pending byte's
    offset from the frame function's code, so a resize has nothing to install or remove.
    Code is static for the life of the process, so each scan runs once. Every way this can
    fail raises before anything is written.

    Raises:
        VideoManagerError: The code or the manager is not what this expects
        PatternFailed: A pattern has no match, on a build it no longer fits
        PatternMultipleResults: A pattern has more than one match
    """

    def __init__(self, hook_handler):
        super().__init__(hook_handler)
        self._slot = None
        self._frame_function = None
        self._pending_flag_offset = None
        self._checked_manager = None

    async def _read_frame_function(self) -> int:
        if self._frame_function is None:
            self._frame_function = await self.pattern_scan(_FRAME_PATTERN, module=_EXE_NAME)
        return self._frame_function

    async def _read_slot(self) -> int:
        if self._slot is None:
            match = await self.pattern_scan(_SLOT_PATTERN, module=_EXE_NAME)
            displacement = await self.read_typed(match + _SLOT_DISPLACEMENT_AT, Primitive.int32)
            self._slot = match + _SLOT_INSTRUCTION_LENGTH + displacement
        return self._slot

    async def read_base_address(self) -> int:
        """
        The video manager, once its vtable has been seen to hold the frame function

        A write of the pending byte lands wherever this says, with no error if it is wrong.
        """
        manager = await self.read_typed(await self._read_slot(), Primitive.int64)
        if not manager:
            raise VideoManagerError("The video manager's slot is empty")

        if manager != self._checked_manager:
            frame_function = await self._read_frame_function()
            vtable = await self.read_typed(manager, Primitive.int64)
            entries = await self.read_bytes(vtable, _VTABLE_ENTRIES * 8)
            if frame_function not in struct.unpack(f"<{_VTABLE_ENTRIES}Q", entries):
                raise VideoManagerError(
                    f"The object at {manager:#x} has no frame function in its vtable"
                )
            self._checked_manager = manager

        return manager

    async def pending_flag_offset(self) -> int:
        """
        Where the pending byte is on the manager, read from the code that tests and clears it

        A patch moves it, and the old offset is some other byte, so both sites have to agree.
        """
        if self._pending_flag_offset is None:
            frame_function = await self._read_frame_function()
            tested = _displacement(
                await self.read_bytes(frame_function + _PENDING_TEST_AT, 7),
                b"\x80\xB9",
                _PENDING_TEST_AT,
            )
            cleared = _displacement(
                await self.read_bytes(frame_function + _PENDING_CLEAR_AT, 7),
                b"\xC6\x83",
                _PENDING_CLEAR_AT,
            )
            if tested != cleared:
                raise VideoManagerError(
                    f"The pending byte is tested at {tested:#x} and cleared at {cleared:#x}"
                )
            self._pending_flag_offset = tested
        return self._pending_flag_offset

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
