from wizwalker import MemoryReadError
from wizwalker.memory.memory_objects.window import window_at_path, windows_at_path

from ...fake_windows import Window


class Unreadable(Window):
    async def name(self):
        raise MemoryReadError(0)


async def test_a_path_that_branches_backs_out_of_the_branch_that_does_not_complete_it():
    target = Window("C")
    root = Window("root", children=[Window("A", children=[Window("B")]), Window("A", children=[Window("B", children=[target])])])

    assert await window_at_path(root, ("A", "B", "C")) is target
    assert await window_at_path(root, ("A", "X")) is None


async def test_every_branch_that_completes_a_path_is_found_in_tree_order():
    first, second = Window("C"), Window("C")
    root = Window("root", children=[Window("A", children=[first]), Window("B"), Window("A", children=[second])])

    assert await windows_at_path(root, ("A", "C")) == [first, second]
    assert await window_at_path(root, ("A", "C")) is first


async def test_an_empty_path_is_the_window_itself():
    root = Window("root")
    assert await window_at_path(root, ()) is root


async def test_an_unreadable_window_is_a_branch_that_does_not_complete_the_path():
    target = Window("A")
    root = Window("root", children=[Unreadable("A"), target])

    assert await windows_at_path(root, ("A",)) == [target]
