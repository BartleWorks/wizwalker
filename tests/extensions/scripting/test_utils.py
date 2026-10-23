import asyncio

import pytest

from wizwalker.extensions.scripting import utils
from wizwalker.utils import Rectangle

from ...fake_windows import Window

# Never anyone's: test names stay synthetic
FRIENDS = [f"Friend {n}" for n in range(1, 26)]


def entry(name: str, row: int, icon: int = 3, icon_list: int = 1) -> str:
    y = row * 30
    return (
        f"<Y;{y}><X;0><indent;0><Color;FFFFFFFF><left>"
        f"<icon;FriendsList/Friend_Icon_List_0{icon_list}.dds;20;20;{icon}></left>"
        f"<Y;{y}><X;24><indent;0><Color;FFFFFFFF><COLOR;FFFFFFFF>{name}"
    )


def message_box() -> Window:
    """A modal message box, hidden, laid out as the game's."""
    return Window("MessageBoxModalWindow", visible=False, children=[
        Window("messageBoxBG", children=[Window("messageBoxLayout", children=[
            Window("TextArea", children=[Window("CaptionText")]),
            Window("AdjustmentWindow", children=[Window("Layout", children=[
                Window("leftButton"), Window("centerButton"), Window("rightButton"),
            ])]),
        ])]),
    ])


class RenderContext:
    async def ui_scale(self):
        return 1.0


class FriendsGame:
    """The friends list, its profile popup and the confirm box, answering clicks."""

    def __init__(self, friends=FRIENDS, *, page: int = 1, list_open: bool = True):
        self.friends = list(friends)
        self.page = page
        self.pages = max(1, -(-len(self.friends) // 10))
        self.ported_to = None
        self.clicks = []
        self.render_context = RenderContext()
        self.mouse_handler = self

        self.root_window = Window("root")
        hud = self.root_window.add(Window("WorldView")).add(Window("windowHUD"))
        self.friends_window = hud.add(Window("NewFriendsListWindow", visible=list_open))
        self.friends_window.add(Window("lblFriendsList", text="<center>Online Friends</center>"))
        self.friends_window.add(Window("btnListTypeRight"))
        layout = self.friends_window.add(Window("ListLayout"))
        self.list = layout.add(Window(
            "listFriends",
            text="\n".join(entry(name, i % 10) for i, name in enumerate(self.friends)),
            rect=Rectangle(100, 200, 300, 500),
        ))
        self.friends_window.add(Window("btnArrowDown"))
        self.friends_window.add(Window("btnArrowUp"))
        self.page_number = self.friends_window.add(Window("PageNumber"))
        self._show_page()
        hud.add(Window("windowForTopRowButtons", children=[Window("btnFriends")]))
        self.profile = hud.add(Window("wndCharacter", visible=False, children=[
            Window("lblCharacterName"), Window("btnGoToFriend"), Window("btnCharacterClose"),
        ]))
        self.box = self.root_window.add(message_box())
        self.yes = self.box.find("centerButton")
        # Another box, as the deck page's: a search by name finds two
        self.root_window.find("WorldView").add(message_box())

    def _show_page(self):
        self.page_number.text = f"<center>{self.page} / {self.pages}</center>"

    def show_profile(self, name):
        self.profile.visible = True
        self.profile.find("lblCharacterName").text = name

    # --- the mouse ---------------------------------------------------------

    async def click_window(self, window, **kwargs):
        name = await window.name()
        self.clicks.append(name)
        if name == "btnArrowDown" and self.page < self.pages:
            self.page += 1
        elif name == "btnArrowUp" and self.page > 1:
            self.page -= 1
        elif name == "btnFriends":
            self.friends_window.visible = not self.friends_window.visible
        elif name == "btnGoToFriend":
            self.box.visible = True
            self.box.find("CaptionText").text = "Teleport to your friend?"
        elif window is self.yes and self.box.visible:
            self.ported_to = self.profile.find("lblCharacterName").text
            self.box.visible = False
            self.box.find("CaptionText").text = None
        elif name == "btnCharacterClose":
            self.profile.visible = False
        self._show_page()

    async def click(self, x, y, **kwargs):
        rect = self.list.rect
        if rect.x1 <= x <= rect.x2 and rect.y1 <= y <= rect.y2:
            row = (y - rect.y1) // 30
            self.clicks.append(f"row {row}")
            self.show_profile(self.friends[(self.page - 1) * 10 + row])


@pytest.fixture
def short_waits(monkeypatch):
    monkeypatch.setattr(utils, "FRIENDS_LIST_TIMEOUT", 0.1)


@pytest.mark.parametrize("friend", ["Friend 3", "Friend 13", "Friend 25"])
async def test_ports_to_the_friend_named_from_the_first_page(friend):
    game = FriendsGame()
    await asyncio.wait_for(utils.teleport_to_friend_from_list(game, name=friend), 10)
    assert game.ported_to == friend


@pytest.mark.parametrize("friend", ["Friend 3", "Friend 13"])
async def test_ports_to_a_friend_on_an_earlier_page(friend):
    # The list was left on its last page. The row was clicked there, and whoever
    # sat in it was ported to instead.
    game = FriendsGame(page=3)
    await asyncio.wait_for(utils.teleport_to_friend_from_list(game, name=friend), 10)
    assert game.ported_to == friend
    assert "btnArrowUp" in game.clicks


async def test_opens_the_list_when_it_is_closed():
    game = FriendsGame(list_open=False)
    await asyncio.wait_for(utils.teleport_to_friend_from_list(game, name="Friend 7"), 10)
    assert game.clicks[0] == "btnFriends"
    assert game.ported_to == "Friend 7"


async def test_closes_the_list_after():
    game = FriendsGame()
    await utils.teleport_to_friend_from_list(game, name="Friend 1")
    assert not game.friends_window.visible
    assert not game.profile.visible


async def test_a_friend_not_online_is_a_value_error():
    game = FriendsGame()
    with pytest.raises(ValueError):
        await utils.teleport_to_friend_from_list(game, name="Friend 99")
    assert game.ported_to is None


async def test_no_friends_online_is_a_value_error():
    game = FriendsGame(friends=[])
    game.list.text = ""
    with pytest.raises(ValueError):
        await utils.teleport_to_friend_from_list(game, name="Friend 1")


@pytest.mark.parametrize("kwargs", [{}, {"icon_list": 1}, {"icon_index": 3}])
async def test_refuses_an_incomplete_lookup(kwargs):
    with pytest.raises(ValueError):
        await utils.teleport_to_friend_from_list(FriendsGame(), **kwargs)


async def test_finds_a_friend_by_icon():
    game = FriendsGame()
    game.list.text = "\n".join(
        entry(name, i, icon=5 if name == "Friend 4" else 3) for i, name in enumerate(game.friends[:10])
    )
    await utils.teleport_to_friend_from_list(game, icon_list=1, icon_index=5)
    assert game.ported_to == "Friend 4"


async def test_paging_waits_for_each_page_to_turn(short_waits):
    game = FriendsGame()
    turned = []
    loop = asyncio.get_running_loop()

    def turn():
        game.page += 1
        game._show_page()

    async def slow_pages(window, **kwargs):
        # The game shows the new page a moment after the click, and a click sent
        # before then pages from the old one
        assert await window.name() == "btnArrowDown"
        turned.append(game.page)
        loop.call_later(0.03, turn)

    game.click_window = slow_pages
    match, index = await utils._cycle_friends_list(
        game, game.friends_window.find("btnArrowDown"), game.list, None, None, "Friend 25", 1
    )
    assert index == 24 and game.page == 3
    assert turned == [1, 2]


async def test_the_profile_wait_ends_when_the_popup_opens():
    game = FriendsGame()
    loop = asyncio.get_running_loop()
    started = loop.time()
    await utils._click_on_friend(game, game.list, 4)
    assert game.profile.visible
    assert loop.time() - started < 0.5  # it slept a full second


async def test_the_profile_wait_sees_one_friend_replace_another():
    game = FriendsGame()
    game.show_profile("Friend 1")
    await utils._click_on_friend(game, game.list, 4)
    assert game.profile.find("lblCharacterName").text == "Friend 5"


async def test_the_profile_wait_gives_up_when_nothing_changes(short_waits):
    game = FriendsGame()
    game.show_profile("Friend 5")
    loop = asyncio.get_running_loop()
    started = loop.time()
    await utils._click_on_friend(game, game.list, 4)
    assert 0.09 <= loop.time() - started < 0.5


def test_reads_the_page_number():
    assert utils._parse_page_number("<center>2 / 3</center>") == (2, 3)


async def test_the_message_box_wait_ends_when_a_caption_shows():
    game = FriendsGame()
    game.box.visible = True
    game.box.find("CaptionText").text = "Teleport?"
    await asyncio.wait_for(utils._wait_for_message_box(game, timeout=5, box_path=utils.MESSAGE_BOX_PATH), 1)


async def test_the_message_box_wait_gives_up_quietly():
    await utils._wait_for_message_box(FriendsGame(), timeout=0.05)


async def test_a_message_box_named_by_its_path_is_read_beside_another():
    # Two boxes: a search of the whole UI by name could not tell which to read
    game = FriendsGame()
    other = game.root_window.find("WorldView").add(Window("Other", children=[
        Window("MessageBoxModalWindow", children=[Window("CaptionText", text="Leave the dungeon?")]),
    ]))
    assert await utils._message_box_caption(game) is None
    assert await utils._message_box_caption(game, ("WorldView", "Other", "MessageBoxModalWindow")) == "Leave the dungeon?"
    assert await utils._message_box_caption(game, ("WorldView", "Missing")) is None
    other.find("CaptionText").text = None
    await utils._wait_for_message_box(game, timeout=0.05, box_path=("WorldView", "Other", "MessageBoxModalWindow"))


async def test_the_hud_is_read_by_its_paths():
    game = FriendsGame(list_open=False)
    root = game.root_window
    # Windows a search by name finds first
    root.add(Window("wndCharacter"))
    root.add(Window("btnFriends"))
    root.forbid_searches()
    await asyncio.wait_for(utils.teleport_to_friend_from_list(game, name="Friend 7"), 10)
    assert game.ported_to == "Friend 7"
    assert game.clicks[0] == "btnFriends"


async def test_a_window_at_a_path_is_retried_then_refused(monkeypatch):
    sleeps = []

    async def no_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(utils.asyncio, "sleep", no_sleep)
    with pytest.raises(ValueError):
        await utils._maybe_get_window_at_path(Window("root"), ("A", "Missing"), retries=2)
    assert sleeps == [0.4, 0.4]


async def test_a_named_window_is_retried_then_refused(monkeypatch):
    sleeps = []

    async def no_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(utils.asyncio, "sleep", no_sleep)
    with pytest.raises(ValueError):
        await utils._maybe_get_named_window(Window("root"), "Missing", retries=2)
    assert sleeps == [0.4, 0.4]


async def test_a_name_found_twice_is_refused():
    root = Window("root", children=[Window("Twin"), Window("Twin")])
    with pytest.raises(ValueError):
        await utils._maybe_get_named_window(root, "Twin", retries=0)
