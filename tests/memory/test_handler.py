import pytest
import regex

from wizwalker import PatternFailed
from wizwalker.memory.handler import HookHandler

OLD_COMPILER, NEW_COMPILER = HookHandler.AUTOBOT_PATTERNS

# AutobotManager::Initialize's opening as the old compiler builds it (r806919, r812087,
# r814353), assembled here with made-up displacements and immediates: the instruction
# forms are the client's, the operands not.
OLD_PROLOGUE = bytes.fromhex(
    "48 89 5c 24 10 48 89 74 24 18 48 89 7c 24 20"  # mov [rsp+?], rbx / rsi / rdi
    "55 41 54 41 55 41 56 41 57"  # push rbp, r12, r13, r14, r15
    "48 8d ac 24 80 fe ff ff"  # lea rbp, [rsp-0x180]
    "48 81 ec 80 02 00 00"  # sub rsp, 0x280
    "48 8b 05 11 11 11 11 48 33 c4 48 89 85 70 01 00 00"  # the stack cookie
)
OLD_BODY_OPENING = bytes.fromhex(
    "4c 8b f1"  # mov r14, rcx
    "48 8b 05 22 22 22 22"  # mov rax, [rip+?]
    "80 b8 34 12 02 00 00"  # cmp byte [rax+0x21234], 0
    "0f 84 00 01 00 00"  # je
)
# And as the new compiler builds it (r811339, r813697)
NEW_PROLOGUE = bytes.fromhex(
    "48 8b c4"  # mov rax, rsp
    "55 41 54 41 55 41 56 41 57"  # push rbp, r12, r13, r14, r15
    "48 8d a8 00 fe ff ff"  # lea rbp, [rax-0x200]
    "48 81 ec 00 03 00 00"  # sub rsp, 0x300
    "48 c7 45 c0 fe ff ff ff"  # mov qword [rbp-0x40], -2
    "48 89 58 10 48 89 70 18 48 89 78 20"  # mov [rax+?], rbx / rsi / rdi
    "48 8b 05 11 11 11 11 48 33 c4 48 89 85 a0 01 00 00"  # the stack cookie
)
NEW_BODY_OPENING = bytes.fromhex(
    "4c 8b e9"  # mov r13, rcx
    "48 8b 05 22 22 22 22"  # mov rax, [rip+?]
    "80 b8 34 12 00 00 00"  # cmp byte [rax+0x1234], 0
    "0f 84 00 01 00 00"  # je
)
#: The function's length from each compiler, from the exes' .pdata.
OLD_INITIALIZE_SIZE = 4188
NEW_INITIALIZE_SIZE = 3975

OTHER_BODY = bytes.fromhex("48 89 cb 48 8b 0d 33 33 33 33 e8 44 44 44 44")
PADDING = b"\xcc" * 16


def _scan(pattern: bytes, data: bytes) -> list[int]:
    # As MemoryReader scans a page
    return [m.start() for m in regex.finditer(pattern, data, regex.DOTALL)]


def test_the_old_compilers_pattern_finds_initialize_as_it_builds_it():
    assert _scan(OLD_COMPILER, PADDING + OLD_PROLOGUE + OLD_BODY_OPENING + PADDING) == [16]


def test_the_new_compilers_pattern_finds_initialize_as_it_builds_it():
    assert _scan(NEW_COMPILER, PADDING + NEW_PROLOGUE + NEW_BODY_OPENING + PADDING) == [16]


@pytest.mark.parametrize(
    "pattern, function",
    [
        (OLD_COMPILER, NEW_PROLOGUE + NEW_BODY_OPENING),
        (NEW_COMPILER, OLD_PROLOGUE + OLD_BODY_OPENING),
    ],
    ids=["old pattern on a new build", "new pattern on an old build"],
)
def test_neither_pattern_matches_the_other_compilers_build(pattern, function):
    assert _scan(pattern, PADDING + function + PADDING) == []


@pytest.mark.parametrize(
    "pattern, prologue",
    [(OLD_COMPILER, OLD_PROLOGUE), (NEW_COMPILER, NEW_PROLOGUE)],
    ids=["old", "new"],
)
def test_a_pattern_needs_the_bodys_opening_not_just_the_prologue(pattern, prologue):
    # The prologue alone opens hundreds of the client's functions.
    assert _scan(pattern, PADDING + prologue + OTHER_BODY + PADDING) == []


def test_the_whole_autobot_fits_in_the_function_it_overwrites_on_either_build():
    # All AUTOBOT_SIZE bytes are zeroed when the cave is prepared, so a size past the
    # function's end overwrites the code after it.
    assert HookHandler.AUTOBOT_SIZE <= min(OLD_INITIALIZE_SIZE, NEW_INITIALIZE_SIZE)


CAVE = 0x1400_0000


@pytest.fixture
def client_exe(process, monkeypatch):
    """Lays out one build's Initialize, and scans for patterns over it as the client would."""

    def install(function: bytes):
        process.map(CAVE, function, size=HookHandler.AUTOBOT_SIZE)

        async def scan(self, pattern, *, module=None, return_multiple=False):
            found = _scan(pattern, function)
            if not found:
                raise PatternFailed(pattern)
            return CAVE + found[0]

        monkeypatch.setattr(HookHandler, "pattern_scan", scan)

    return install


@pytest.mark.parametrize(
    "function, pattern",
    [
        (OLD_PROLOGUE + OLD_BODY_OPENING, OLD_COMPILER),
        (NEW_PROLOGUE + NEW_BODY_OPENING, NEW_COMPILER),
    ],
    ids=["old", "new"],
)
async def test_the_cave_is_found_on_either_build_and_put_back_as_it_was(
    process, hook_handler, client_exe, function, pattern
):
    client_exe(function)

    await hook_handler._prepare_autobot()
    assert hook_handler._autobot_address == CAVE
    assert hook_handler._autobot_pattern is pattern
    assert process.read_bytes(CAVE, HookHandler.AUTOBOT_SIZE) == bytes(HookHandler.AUTOBOT_SIZE)

    await hook_handler.close()
    restored = process.read_bytes(CAVE, len(function))
    assert restored == function
    assert hook_handler._autobot_pattern is None


async def test_a_build_neither_pattern_matches_raises(hook_handler, client_exe):
    client_exe(OTHER_BODY)
    with pytest.raises(RuntimeError, match="every autobot pattern"):
        await hook_handler._prepare_autobot()
