from typing import Union, Optional

from wizwalker.memory.memory_object import Primitive, MemoryObject
from .camera_controller import (
    DynamicCameraController,
    CameraController,
    DynamicFreeCameraController,
    DynamicElasticCameraController,
)
from .client_object import DynamicClientObject
from .character_registry import DynamicCharacterRegistry
from .enums import AccountPermissions
from .gamebryo_presenter import DynamicGamebryoPresenter
from .fishing_manager import FishingManager


# The camera fields are found from the client's own camera switches, by patterns that name
# none of the offsets they read, so a patch that moves the fields is followed, not missed.
# The fallbacks are r814353's. Each pattern matches once on r812087 and r814353.
#
# Selecting the elastic camera: `mov rdx, [rbx+elastic]; call [rax+?]; mov byte [rbx+freecam], 0`.
SELECT_ELASTIC_PATTERN = rb"\x48\x8B\x93....\xFF\x90....\xC6\x83....\x00"
# Selecting the free camera: `mov rcx, [rbx+free]; call; mov byte [rbx+freecam], 1; mov byte [rbx+?], 1`.
SELECT_FREE_PATTERN = rb"\x48\x8B\x8B....\xE8....\xC6\x83....\x01\xC6\x83....\x01"
# The camera toggle: `cmp [rbx+selected], rdx; jne; mov rdx, [rbx+elastic]; call [rax+?]`.
SELECTED_IS_ELASTIC_PATTERN = rb"\x48\x39\x93....\x75.\x48\x8B\x93....\xFF\x90...."
# The freecam flag's clear in SELECT_ELASTIC_PATTERN, matched from its own `mov byte`.
FREECAM_CLEAR_PATTERN = rb"(?<=\x48\x8B\x93....\xFF\x90....)\xC6\x83....\x00"


# note: not defined
class GameClient(MemoryObject):
    async def read_base_address(self) -> int:
        raise NotImplementedError()

    async def elastic_camera_controller(self) -> Optional[DynamicElasticCameraController]:
        offset = await self.pattern_scan_offset_cached(
            SELECT_ELASTIC_PATTERN,
            3,
            "elastic_camera_controller",
            0x222D0
        )

        addr = await self.read_value_from_offset(offset, Primitive.uint64)

        if addr == 0:
            return None

        return DynamicElasticCameraController(self.hook_handler, addr)

    async def select_camera_slot(self) -> int:
        """
        The client's vtable slot that makes a camera controller the selected one, read from
        the call in the client's own switch to the elastic camera
        """
        # `\xFF\x90` and its displacement start 7 bytes into SELECT_ELASTIC_PATTERN
        return await self.pattern_scan_offset_cached(
            SELECT_ELASTIC_PATTERN,
            9,
            "select_camera_slot",
            0x498
        )

    async def free_camera_controller(self) -> Optional[DynamicFreeCameraController]:
        offset = await self.pattern_scan_offset_cached(
            SELECT_FREE_PATTERN,
            3,
            "free_camera_controller",
            0x222E0
        )

        addr = await self.read_value_from_offset(offset, Primitive.uint64)

        if addr == 0:
            return None

        return DynamicFreeCameraController(self.hook_handler, addr)

    async def selected_camera_controller(self) -> Optional[DynamicCameraController]:
        """
        The in use camera controller
        """
        offset = await self.pattern_scan_offset_cached(
            SELECTED_IS_ELASTIC_PATTERN,
            3,
            "selected_camera_controller",
            0x22300
        )

        addr = await self.read_value_from_offset(offset, Primitive.uint64)

        if addr == 0:
            return None

        return DynamicCameraController(self.hook_handler, addr)

    async def write_selected_camera_controller(self, selected_camera_controller: Union[CameraController, int]):
        """
        Write the in use camera controller
        """
        if isinstance(selected_camera_controller, CameraController):
            selected_camera_controller = await selected_camera_controller.read_base_address()

        offset = await self.pattern_scan_offset_cached(
            SELECTED_IS_ELASTIC_PATTERN,
            3,
            "selected_camera_controller",
            0x22300
        )

        await self.write_value_to_offset(offset, selected_camera_controller, Primitive.uint64)

    async def is_freecam(self) -> bool:
        """
        If the game is currently in freecam mode
        """
        offset = await self.pattern_scan_offset_cached(
            FREECAM_CLEAR_PATTERN,
            2,
            "is_freecam",
            0x22318
        )
        return await self.read_value_from_offset(offset, Primitive.bool)

    async def write_is_freecam(self, is_freecam: bool):
        """
        Write if the game is currently in freecam mode
        """
        offset = await self.pattern_scan_offset_cached(
            FREECAM_CLEAR_PATTERN,
            2,
            "is_freecam",
            0x22318
        )
        await self.write_value_to_offset(offset, is_freecam, Primitive.bool)

    async def root_client_object(self) -> Optional[DynamicClientObject]:
        """
        The root client object, all other client objects are its children
        """
        # A shared pointer the client copies in some 170 places, none of them telling enough
        # to anchor a pattern on, so it is read where r814353 keeps it: the top of the
        # player's parent chain, found there in a live client.
        addr = await self.read_value_from_offset(0x21378, Primitive.uint64)

        if addr == 0:
            return None

        return DynamicClientObject(self.hook_handler, addr)

    async def frames_per_second(self) -> float:
        """
        The number of frames processed the last second, updated every 5 seconds by default
        """
        offset = await self.pattern_scan_offset_cached(
            rb"\xF3\x0F\x11\x8B....\xC7\x05........\xF2\x0F"
            rb"\x11.....\x48\x8B\x8B....\x48\x85\xC9\x74\x09",
            4,
            "frames_per_second",
            0x21A7C
        )
        return await self.read_value_from_offset(offset, Primitive.float32)

    async def shutdown_signal(self) -> int:
        """
        Signal used to check if the main loop should close
        """
        offset = await self.pattern_scan_offset_cached(
            rb"\x38\x9F\xB8\x11\x02\x00\x74\xBE\xE8....\x83\xF8\x64\x0F\x8F....\xB9\x0F\x00\x00\x00",
            2,
            "shutdown_signal",
            0x211B8
        )
        return await self.read_value_from_offset(offset, Primitive.int32)

    async def write_shutdown_signal(self, shutdown_signal: int):
        """
        Writing 1 into the shutdown signal will close the program (exits main loop)
        """
        offset = await self.pattern_scan_offset_cached(
            rb"\x38\x9F\xB8\x11\x02\x00\x74\xBE\xE8....\x83\xF8\x64\x0F\x8F....\xB9\x0F\x00\x00\x00",
            2,
            "shutdown_signal",
            0x211B8
        )
        await self.write_value_to_offset(offset, shutdown_signal, Primitive.int32)

    # The ids below are r814353's, each checked in a live client: user_id is what the
    # login handler stores beside "UserID" and "SupportID", player_gid what the client
    # compares its own objects' ids against ("Our client is getting hurt!") and equal to
    # the player's global id, machine_id what it logs as "MachineID: %llu".

    async def user_id(self) -> int:
        """Account-level UserID from MSG_USER_VALIDATE / MSG_CHARACTERSELECTED."""
        return await self.read_value_from_offset(0x21258, Primitive.uint64)

    async def player_gid(self) -> int:
        """Current character's global ID (CharID from MSG_CHARACTERSELECTED)."""
        return await self.read_value_from_offset(0x213A8, Primitive.uint64)

    async def machine_id(self) -> int:
        """MachineID sent during login."""
        return await self.read_value_from_offset(0x21508, Primitive.uint64)

    async def character_registry(self) -> Optional[DynamicCharacterRegistry]:
        """
        Get the character registry
        """
        # Where r814353 keeps it, checked against the registry Client.character_registry
        # finds by its own pattern, which is the one to prefer.
        addr = await self.read_value_from_offset(0x227A0, Primitive.uint64)

        if addr == 0:
            return None

        return DynamicCharacterRegistry(self.hook_handler, addr)

    async def account_permissions(self) -> AccountPermissions:
        offset = await self.pattern_scan_offset_cached(
            rb"\x41\x89\x86....\x4D\x8B\x06\x8B"
            rb"\xD0\x49\x8B\xCE\x41\xFF\x90...."
            rb"\x49\x8B\x06\x49\x8B\xCE\xFF\x90....",
            3,
            "account_permissions",
            0x21DC4
        )
        return await self.read_enum(offset, AccountPermissions)

    async def write_account_permissions(self, account_permissions: AccountPermissions):
        offset = await self.pattern_scan_offset_cached(
            rb"\x41\x89\x86....\x4D\x8B\x06\x8B"
            rb"\xD0\x49\x8B\xCE\x41\xFF\x90...."
            rb"\x49\x8B\x06\x49\x8B\xCE\xFF\x90....",
            3,
            "account_permissions",
            0x21DC4
        )
        await self.write_enum(offset, account_permissions)

    async def has_membership(self) -> bool:
        offset = await self.pattern_scan_offset_cached(
            rb"\x83\xBB....\x00\x75\x04\xB2\x01\xEB\x02\x33\xD2\x48\x8B.....\xE8",
            2,
            "has_membership",
            0x21DC8
        )
        return await self.read_value_from_offset(offset, Primitive.bool)

    # no, this doesn't let you go in membership areas
    async def write_has_membership(self, has_membership: bool):
        offset = await self.pattern_scan_offset_cached(
            rb"\x83\xBB....\x00\x75\x04\xB2\x01\xEB\x02\x33\xD2\x48\x8B.....\xE8",
            2,
            "has_membership",
            0x21DC8
        )
        await self.write_value_to_offset(offset, has_membership, Primitive.bool)

    async def gamebryo_presenter(self) -> DynamicGamebryoPresenter:
        """
        Thing used for rendering
        """
        offset = await self.pattern_scan_offset_cached(
            rb".......\x48\x8B\x01\xFF\x50\x40\x84\xC0\x75.\xE8",
            3,
            "gamebryo_presenter",
            0x222B0
        )

        addr = await self.read_value_from_offset(offset, Primitive.uint64)

        if addr == 0:
            return None

        return DynamicGamebryoPresenter(self.hook_handler, addr)

    async def fishing_manager(self) -> FishingManager:
        # A pointer the client calls through (`mov rcx, [rcx+0x231D0]; call`) on r814353;
        # 0x231A8, which this read before, holds an id the client compares against.
        addr = await self.read_value_from_offset(0x231D0, Primitive.uint64)
        return FishingManager(self.hook_handler, addr)

class CurrentGameClient(GameClient):
    _base_address = None

    async def read_base_address(self) -> int:
        if self._base_address is not None:
            return self._base_address

        addr = await self.pattern_scan(rb"\x48\x8b.....\x48\x8b.\x80\xb8....\x00\x74.\x4c\x8b", module="WizardGraphicalClient.exe")
        offset = await self.read_typed(addr + 3, Primitive.int32)

        self._base_address = await self.read_typed(addr + 7 + offset, Primitive.uint64)
        return self._base_address
