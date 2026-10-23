import asyncio

import pytest

from wizwalker import Keycode
from wizwalker.extensions.scripting import deck_builder
from wizwalker.extensions.scripting.deck_builder import DeckBuilder
from wizwalker.utils import Rectangle

from ...fake_windows import Window

OPEN_DELAY = 0.05


class Spellbook:
    """The deck page, opened and closed by P a moment after the key, as the game does."""

    def __init__(self, *, page_open: bool = False, opens: bool = True):
        self.opens = opens
        self.actions = []
        self.mouse_handler = self
        self.root_window = Window("root", children=[
            # Another box ahead of the deck's, which a search by name would find first
            Window("MessageBoxModalWindow", visible=False, children=[Window("CaptionText"), Window("centerButton")]),
        ])
        self.world_view = self.root_window.add(Window("WorldView", children=[
            Window("windowHUD", children=[Window("btnSpellbook")]),
        ]))
        self.box = self.world_view.add(Window("MessageBoxModalWindow", visible=False, children=[
            Window("messageBoxBG", children=[Window("messageBoxLayout", children=[
                Window("TextArea", children=[Window("CaptionText")]),
                Window("AdjustmentWindow", children=[Window("Layout", children=[
                    Window("leftButton"), Window("centerButton"), Window("rightButton"),
                ])]),
            ])]),
        ]))
        self.page = None
        if page_open:
            self._open()

    def _open(self):
        self.page = self.world_view.add(Window("DeckConfiguration", children=[
            Window("Deck"), Window("Cards_All"), Window("TreasureCardButton"), Window("ClearDeckButton"),
        ]))

    def _toggle(self):
        if self.page is None:
            if self.opens:
                self._open()
        else:
            self.world_view.remove(self.page)
            self.page = None

    # --- the client ------------------------------------------------------

    async def send_key(self, key, seconds: float = 0):
        assert key is Keycode.P
        self.actions.append("P")
        asyncio.get_running_loop().call_later(OPEN_DELAY, self._toggle)

    # --- its mouse handler -----------------------------------------------

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def click_window(self, window, **kwargs):
        name = await window.name()
        self.actions.append(name)
        if name == "ClearDeckButton":
            def ask():
                self.box.visible = True
                self.box.find("CaptionText").text = "Clear your deck?"

            asyncio.get_running_loop().call_later(OPEN_DELAY, ask)
        elif name in ("leftButton", "centerButton", "rightButton"):
            whose = "the deck's box" if window in self.box.find("Layout")._children else "another box"
            self.actions[-1] = f"{name} of {whose} ({'up' if self.box.visible else 'not up'})"
            self.box.visible = False
            self.box.find("CaptionText").text = None

    async def click(self, x, y, **kwargs):
        self.actions.append(("click", x, y))


@pytest.fixture
def short_timeouts(monkeypatch):
    monkeypatch.setattr(deck_builder, "DECK_PAGE_TIMEOUT", 0.3)


async def test_opening_presses_p_and_waits_for_the_page():
    book = Spellbook()
    loop = asyncio.get_running_loop()
    started = loop.time()
    await DeckBuilder(book).open_deck_page()
    # The first lookup of a closed page slept 1.6 s through its retries
    assert loop.time() - started < 1.0
    assert book.actions == ["P", "Deck", "Cards_All"]


async def test_an_open_page_is_used_as_it_is():
    book = Spellbook(page_open=True)
    await DeckBuilder(book).open_deck_page()
    assert book.actions == ["Deck", "Cards_All"]


async def test_opening_knows_it_is_on_the_deck_page():
    # Left False, view_deck_cards() switched to the treasure cards; BartleBot set
    # the flag by hand to stop it
    book = Spellbook()
    builder = DeckBuilder(book)
    await builder.open_deck_page()
    await builder.view_deck_cards()
    assert "TreasureCardButton" not in book.actions


async def test_a_page_that_never_opens_is_a_value_error(short_timeouts):
    with pytest.raises(ValueError):
        await DeckBuilder(Spellbook(opens=False)).open_deck_page()


async def test_closing_waits_for_the_page_to_go():
    book = Spellbook()
    builder = DeckBuilder(book)
    await builder.open_deck_page()
    await builder.close_deck_page()
    assert book.page is None
    assert book.actions[-1] == "P"


async def test_the_context_manager_opens_and_closes():
    book = Spellbook()
    async with DeckBuilder(book):
        assert book.page is not None
    assert book.page is None


async def test_refreshing_waits_for_the_close_before_reopening():
    book = Spellbook()
    builder = DeckBuilder(book)
    await builder.open_deck_page()
    closed_page = book.page
    await builder.refresh_deck_page()
    assert book.actions[-2:] == ["P", "P"]
    assert book.page is not None and book.page is not closed_page
    assert builder._deck_config_window is book.page


async def test_clearing_answers_the_box_once_it_is_up():
    # Its windows outlive the box, so they were found, and clicked, before it showed
    book = Spellbook()
    builder = DeckBuilder(book)
    await builder.open_deck_page()
    await builder.clear_deck()
    assert book.actions[-2:] == ["ClearDeckButton", "centerButton of the deck's box (up)"]


async def test_the_deck_page_and_the_spellbook_button_are_found_by_their_paths():
    book = Spellbook()
    book.root_window.add(Window("DeckConfiguration"))  # a search by name finds this one first
    book.root_window.forbid_searches()
    builder = DeckBuilder(book)
    await builder.open_deck_page()
    assert builder._deck_config_window is book.page
    await builder.clear_deck()
    assert book.actions[-1] == "centerButton of the deck's box (up)"
    await builder.close_deck_page()
    assert book.page is None


async def test_clearing_treasure_cards_that_never_clear_gives_up(monkeypatch):
    builder = DeckBuilder(Spellbook())

    async def nothing(*args, **kwargs):
        return None

    async def two_cards():
        return 2

    monkeypatch.setattr(builder, "view_tc_cards", nothing)
    monkeypatch.setattr(builder, "clear_deck_manual", nothing)
    monkeypatch.setattr(builder, "get_deck_count", two_cards)
    with pytest.raises(RuntimeError):
        await builder.clear_deck_tcs()


class Spell:
    def __init__(self, name):
        self._name = name

    async def spell_template(self):
        return self

    async def name(self):
        return self._name


@pytest.fixture
def deck(monkeypatch):
    book = Spellbook(page_open=True)
    builder = DeckBuilder(book)
    names = ["Fire Cat", "Unicorn", "Unicorn", "Unicorn", "Ice Beetle"]

    async def cards():
        return [Spell(n) for n in names]

    async def rect():
        return Rectangle(0, 0, 800, 800)

    async def no_sleep(delay, result=None):
        return result

    monkeypatch.setattr(builder, "get_graphical_deck_cards", cards)
    monkeypatch.setattr(builder, "get_deck_list_rectangle", rect)
    monkeypatch.setattr(deck_builder.asyncio, "sleep", no_sleep)
    return builder, book


async def test_removes_fewer_copies_than_the_deck_holds(deck):
    # `!=` refused anything but every copy
    builder, book = deck
    await builder.remove_by_name("Unicorn", 1)
    assert book.actions == [("click", 150, 50)]


async def test_removing_more_copies_than_the_deck_holds_is_refused(deck):
    builder, book = deck
    with pytest.raises(ValueError):
        await builder.remove_by_name("Unicorn", 4)
    assert book.actions == []


async def test_removing_no_copies_does_nothing(deck):
    builder, book = deck
    await builder.remove_by_name("Storm Lord", 0)
    assert book.actions == []


# --- positions -----------------------------------------------------------------


@pytest.mark.parametrize("index, page, slot", [(0, 0, 1), (5, 0, 6), (6, 1, 1), (13, 2, 2)])
def test_a_cards_page_and_slot(index, page, slot):
    names = [f"card {i}" for i in range(20)]
    assert DeckBuilder(None).calcuate_position_of_card_in_page(names, f"card {index}") == (page, slot)


def test_divides_a_rectangle_row_by_row():
    cells = DeckBuilder(None).divide_rectangle(Rectangle(0, 0, 200, 300), columns=2, rows=3)
    assert [tuple(c) for c in cells[:3]] == [(0, 0, 100, 100), (100, 0, 200, 100), (0, 100, 100, 200)]
    assert len(cells) == 6
