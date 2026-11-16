import regex

from wizwalker.memory.handler import HookHandler

# AutobotManager::Initialize's opening as r811339 compiles it, assembled here with made-up
# displacements and immediates: the instruction forms are the client's, the operands not.
R811339_PROLOGUE = bytes.fromhex(
    "48 8b c4"  # mov rax, rsp
    "55 41 54 41 55 41 56 41 57"  # push rbp, r12, r13, r14, r15
    "48 8d a8 00 fe ff ff"  # lea rbp, [rax-0x200]
    "48 81 ec 00 03 00 00"  # sub rsp, 0x300
    "48 c7 45 c0 fe ff ff ff"  # mov qword [rbp-0x40], -2
    "48 89 58 10 48 89 70 18 48 89 78 20"  # mov [rax+?], rbx / rsi / rdi
    "48 8b 05 11 11 11 11 48 33 c4 48 89 85 a0 01 00 00"  # the stack cookie
)
R811339_BODY_OPENING = bytes.fromhex(
    "4c 8b e9"  # mov r13, rcx
    "48 8b 05 22 22 22 22"  # mov rax, [rip+?]
    "80 b8 34 12 00 00 00"  # cmp byte [rax+0x1234], 0
    "0f 84 00 01 00 00"  # je
)
#: The function's length on r811339, from the exe's .pdata.
R811339_INITIALIZE_SIZE = 3975

PADDING = b"\xcc" * 16


def _scan(data: bytes) -> list[int]:
    # As MemoryReader scans a page
    return [m.start() for m in regex.finditer(HookHandler.AUTOBOT_PATTERN, data, regex.DOTALL)]


def test_the_autobot_pattern_finds_initialize_as_r811339_builds_it():
    assert _scan(PADDING + R811339_PROLOGUE + R811339_BODY_OPENING + PADDING) == [16]


def test_the_autobot_pattern_needs_the_bodys_opening_not_just_the_prologue():
    # The prologue alone opens hundreds of r811339's functions.
    other_body = bytes.fromhex("48 89 cb 48 8b 0d 33 33 33 33 e8 44 44 44 44")
    assert _scan(PADDING + R811339_PROLOGUE + other_body + PADDING) == []


def test_the_whole_autobot_fits_in_the_function_it_overwrites():
    # All AUTOBOT_SIZE bytes are zeroed when the cave is prepared, so a size past the
    # function's end overwrites the code after it.
    assert HookHandler.AUTOBOT_SIZE <= R811339_INITIALIZE_SIZE
