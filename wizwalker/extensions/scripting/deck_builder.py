import math
import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

from wizwalker.extensions.scripting.utils import _maybe_get_named_window, _wait_for_message_box
from wizwalker.memory.memory_object import MemoryReadError
from wizwalker.utils import Rectangle, poll_until
from wizwalker.memory.memory_objects.window import DynamicSpellListControl, DynamicDeckListControl, SpellListControlSpellEntry, DeckListControlSpellEntry, DynamicGraphicalSpellWindow
from wizwalker.memory.memory_objects.spell import DynamicGraphicalSpell
from wizwalker.memory.memory_objects import Window
from wizwalker.memory import Window
from wizwalker import Keycode


@dataclass
class _ItemCardSlot:
    name: str
    page: int        # 0-indexed page number
    slot_index: int  # 0-indexed position in the 8×2 grid (0–15)
    is_active: bool  # True = card is toggled on (in deck)

if TYPE_CHECKING:
    from wizwalker import Client


#: How long the deck page is given to open or close after its P. It takes about
#: a fifth of a second; this only runs out when the keypress was lost.
DECK_PAGE_TIMEOUT = 5.0

#: How often the deck page waits read.
DECK_PAGE_POLL = 0.05


"""
async with DeckBuilder(client) as db:
    db.add(123)

# entire deck config window
--- [DeckConfigurationWindow] SpellBookPrefsPage

# toolbar parent?
---- [ControlSprite] ControlSprite

# top bar buttons
----- [toolbar] Window

# select school
------ [TabBackground] ControlSprite
------ [Cards_Fire] ControlCheckBox
------ [Cards_Ice] ControlCheckBox
------ [Cards_Storm] ControlCheckBox
------ [Cards_Myth] ControlCheckBox
------ [Cards_All] ControlCheckBox
------ [Cards_Life] ControlCheckBox
------ [RightSideTabs] Window
------- [Cards_Death] ControlCheckBox
------- [Cards_Balance] ControlCheckBox
------- [Cards_Astral] ControlCheckBox
------- [Cards_Shadow] ControlCheckBox
------- [Cards_MonsterMagic] ControlCheckBox


# other pages (unrelated)
------ [GoToTieredWindow] Window
------- [GoToTieredGlow] ControlSprite
------- [GoToTiered] ControlCheckBox
------ [GoToGardening] ControlCheckBox
------ [GoToFishing] ControlCheckBox
------ [GoToCantrips] ControlCheckBox
------ [GoToCastleMagic] ControlCheckBox
------ [GoBackToCastleMagic] ControlCheckBox
------ [GoBackToFishing] ControlCheckBox
------ [GoBackToGardening] ControlCheckBox
------ [GoBackToTieredWindow] Window
------- [GoBackToTieredGlow] ControlSprite
------- [GoBackToTiered] ControlCheckBox


# just parent window?
----- [DeckPage] Window

?
------ [PageUp] ControlButton
------ [PageDown] ControlButton

# cards to add to deck (renamed in 2026-06 update)
------ [AllPageSpellList] SpellListControl

# equip icon
------ [EquipBorder] ControlWidget

# ?
------ [InvBorder] ControlWidget

# cards given by items? (most likely)
------ [ItemSpells] DeckListControl

# ?
------ [ControlSprite] ControlSprite

# deck selection
------ [PrevDeck] ControlButton
------ [NextDeck] ControlButton

# deck name
------ [DeckName] ControlText

# equip icon?
------ [equipFist] Window

# spells added to normal deck (may also be used for tc)
------ [CardsInDeck] DeckListControl


# tc info
------ [TreasureCardCountBackground] Window
------ [TreasureCardCount] ControlText
------ [TreasureCardIcon] Window

# rename deck
------ [NewDeckName] ControlButton

# select deck
------ [EquipButton] ControlButton

# next card selection page?
------ [NextItemSpells] ControlButton
------ [PrevItemSpells] ControlButton

# help button
------ [Help] ControlButton

# clear deck (hidden on small decks; try unhiding)
------ [ClearDeckButton] ControlButton

# quick sell tc
------ [QuickSellButton] ControlButton

# ?
----- [ControlSprite] ControlSprite
------ [DeckTitle] ControlText
----- [TutorialLogBackground1] ControlSprite

# switch to tc view
----- [TreasureCardButton] ControlCheckBox


builder.add_card_by_name("unicorn", number_of_copies: int | None)
-> number_of_copies = None: add max copies 
-> raises: ValueError(already at max copies)
-> raises: ValueError(card not found)

builder.remove_card_by_name("unicorn", number_of_copies: int | None)
-> inverse

builder.add_by_predicate(pred, number_of_copies: int | None)
-> see add_card_by_name
def pred(spell: graphical spell):
    return True or False

builder.remove_by_predicate(pred, number_of_copies: int | None)
-> inverse

builder.get_deck_preset() -> dict[...]
{
    normal: {template id: number of copies},
    tc: {template id: number of copies},
    item: {template id: number of copies}
}
-> 


builder.set_deck_preset(dict[see above], ignore_failures: bool = False)
-> removes and adds cards as needed for a preset which is a dict

"""


class DeckBuilder:
    """
    async with DeckBuilder(client) as deck_builder:
        # adds two unicorns
        await deck_builder.add_by_name("Unicorn", 2)
    """

    def __init__(self, client: "Client"):
        self.client = client
        self._deck_config_window = None
        self._on_deck_page = False
        self._deck_open = False

    async def open(self):
        await self.open_deck_page()

    async def close(self):
        await self.close_deck_page()

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    @staticmethod
    def calculate_icon_position(
            card_number: int,
            horizontal_size: int = 33,
            vertical_size: int = 33,
            number_of_rows: int = 8,
            horizontal_spacing: int = 6,
            vertical_spacing: int = 0,
    ):
        x = (horizontal_size * card_number) - (horizontal_size // 2) + \
            (horizontal_spacing * (card_number - 1))
        y = (vertical_size * (((card_number - 1) // number_of_rows) + 1))\
            - (vertical_size // 2) + \
            (vertical_spacing * ((card_number - 1) // number_of_rows))
        return x, y

    async def _find_deck_config_window(self):
        """The deck page's window, or None while the page is closed.

        No retries: the answer is often no, and the default ladder slept 1.6 s to
        give it. The window leaves the tree when the page closes.
        """
        try:
            return await _maybe_get_named_window(self.client.root_window, "DeckConfiguration", retries=0)
        except ValueError:
            return None

    async def _wait_for_deck_page(self, *, open: bool):
        """Wait for the deck page to open or close; returns its window, or None."""
        async def _there():
            self._deck_config_window = await self._find_deck_config_window()
            return (self._deck_config_window is not None) == open

        await poll_until(_there, timeout=DECK_PAGE_TIMEOUT, interval=DECK_PAGE_POLL)
        return self._deck_config_window

    async def _press_spellbook_key(self):
        async with self.client.mouse_handler:
            await self.client.send_key(Keycode.P)

    async def open_deck_page(self) -> None:
        """
        Opens deck page
        """
        if self._deck_open:
            return
        self._deck_config_window = await self._find_deck_config_window()

        if not self._deck_config_window:
            # in game, or the key would open nothing
            await _maybe_get_named_window(self.client.root_window, "btnSpellbook")
            await self._press_spellbook_key()
            if await self._wait_for_deck_page(open=True) is None:
                raise ValueError("No child window named DeckConfiguration")

        deck_button = await _maybe_get_named_window(self._deck_config_window, "Deck")
        async with self.client.mouse_handler:
            await self.client.mouse_handler.click_window(deck_button)
        cards_all = await _maybe_get_named_window(self._deck_config_window, "Cards_All")
        if await cards_all.is_visible():
            async with self.client.mouse_handler:
                await self.client.mouse_handler.click_window(cards_all)
        self._deck_open = True
        # The Deck tab was just clicked. Left False, view_deck_cards() "corrected"
        # it by switching to the treasure cards, and callers had to set it by hand.
        self._on_deck_page = True

    async def close_deck_page(self) -> None:
        if not self._deck_open:
            return
        self._deck_config_window = await self._find_deck_config_window()

        if self._deck_config_window:
            await _maybe_get_named_window(self.client.root_window, "btnSpellbook")
            await self._press_spellbook_key()
            await self._wait_for_deck_page(open=False)
        # NOTE: True because the next time you open deck, it has to be on deck page
        self._on_deck_page = True
        self._deck_open = False

    async def refresh_deck_page(self) -> None:
        # Each P waited for: the second used to follow the first at once, and the
        # page was then looked for while it was still opening
        if self._deck_open:
            await self._press_spellbook_key()
            await self._wait_for_deck_page(open=False)
        await self._press_spellbook_key()
        self._deck_open = True
        self._on_deck_page = True
        if await self._wait_for_deck_page(open=True) is None:
            raise ValueError("No child window named DeckConfiguration")
        # spellbook = await _maybe_get_named_window(self.client.root_window, "btnSpellbook")
        # async with self.client.mouse_handler:
        #     if self._deck_open:
        #         await self.client.mouse_handler.click_window(spellbook)
        #     await self.client.mouse_handler.click_window(spellbook)
        #     self._deck_open = True
        #     self._on_deck_page = True
        # self._deck_config_window = await _maybe_get_named_window(self.client.root_window, "DeckConfiguration")

    async def switch_card_type_window(self) -> None:
        await self.open_deck_page()
        # Above clicks all cards so we dont need to do it again
        treasure_card_button = await _maybe_get_named_window(self._deck_config_window, "TreasureCardButton")
        async with self.client.mouse_handler:
            await self.client.mouse_handler.click_window(treasure_card_button)
        await asyncio.sleep(0.5)
        self._deck_config_window = await _maybe_get_named_window(self.client.root_window, "DeckConfiguration")
        self._on_deck_page = not self._on_deck_page

    async def view_deck_cards(self) -> None:
        if not self._on_deck_page:
            await self.switch_card_type_window()

    async def view_tc_cards(self) -> None:
        if self._on_deck_page:
            await self.switch_card_type_window()

    async def get_spell_list(self) -> list[SpellListControlSpellEntry]:
        spell_list_window = await _maybe_get_named_window(self._deck_config_window, "AllPageSpellList")
        spell_list_control = DynamicSpellListControl(self.client.hook_handler, await spell_list_window.read_base_address())
        list_of_spell_entries = await spell_list_control.spell_entries()
        list_of_valid_spell_entries = []
        # We are doing this to check for valid spells. Sometimes the returned value isn't valid
        for spell in list_of_spell_entries:
            try:
                graphical = await spell.graphical_spell()
                if (not graphical):
                    continue
                template = await graphical.spell_template()
                if (not template):
                    continue
                await template.name()
                list_of_valid_spell_entries.append(spell)
            except Exception:
                pass
        return list_of_valid_spell_entries

    async def get_tiered_spell_list(self) -> list[SpellListControlSpellEntry]:
        """Get valid spell entries from the TieredSpellMPUnlockedList."""
        tiered_window = await _maybe_get_named_window(self._deck_config_window, "TieredSpellMPUnlockedList")
        tiered_control = DynamicSpellListControl(
            self.client.hook_handler, await tiered_window.read_base_address()
        )
        list_of_spell_entries = await tiered_control.spell_entries()
        list_of_valid_spell_entries = []
        for spell in list_of_spell_entries:
            try:
                graphical = await spell.graphical_spell()
                if not graphical:
                    continue
                template = await graphical.spell_template()
                if not template:
                    continue
                await template.name()
                list_of_valid_spell_entries.append(spell)
            except Exception:
                pass
        return list_of_valid_spell_entries

    async def tiered_spell_list_match_template(self, template_name: str) -> list[SpellListControlSpellEntry]:
        """Search the TieredSpellMPUnlockedList for a spell by template name."""
        async def tiered_get_cards_with_predicate(pred: Any) -> list:
            cards = []
            spell_list = await self.get_tiered_spell_list()
            for spell in spell_list:
                if await pred(spell):
                    cards.append(spell)
            return cards
        return await self._pred_match_template_name(tiered_get_cards_with_predicate, template_name)

    async def get_tiered_spell_list_rectangle(self) -> Rectangle:
        """Get the rectangle for the TieredSpellMPUnlockedList window."""
        tiered_list = await _maybe_get_named_window(self._deck_config_window, "TieredSpellMPUnlockedList")
        return await tiered_list.scale_to_client()

    async def set_tiered_spell_page(self, page_number: int):
        """Set the page for the TieredSpellMPUnlockedList."""
        tiered_window = await _maybe_get_named_window(self._deck_config_window, "TieredSpellMPUnlockedList")
        tiered_control = DynamicSpellListControl(
            self.client.hook_handler, await tiered_window.read_base_address()
        )
        await tiered_control.write_start_index(page_number * 6)

    async def get_graphical_tiered_spell_cards(self) -> list[DynamicGraphicalSpell]:
        """Get graphical spell objects from the tiered spell list."""
        list_of_spell_entries = await self.get_tiered_spell_list()
        list_of_spell_graphicals = []
        for spell in list_of_spell_entries:
            graphical = await spell.graphical_spell()
            list_of_spell_graphicals.append(graphical)
        return list_of_spell_graphicals

    async def calculate_tiered_spell_card_position(self, card_number) -> tuple[int, int]:
        """Calculate click position for a card in the tiered spell list.
        The TieredSpellMPUnlockedList uses a 6-slot grid but the first 2
        slots are spacers. Real cards are in slots 3-6 (1-based).
        """
        tiered_rect = await self.get_tiered_spell_list_rectangle()
        rectangle_list = self.divide_rectangle(tiered_rect)
        # Offset by 2 slots for the spacers
        card_rectangle = rectangle_list[card_number - 1 + 2]
        return card_rectangle.center()

    async def get_deck_count(self) -> int:
        spell_slot_rect = await self.get_deck_list_rectangle()
        spell_slots = self.divide_rectangle(spell_slot_rect, 8, 8)
        min = 0
        max = 64
        idx = int(max/2)
        ever_found = False
        async with self.client.mouse_handler:
            while True:
                found = False
                await self.client.mouse_handler.set_mouse_position(*spell_slots[idx].center())
                world_view = await self.client.get_world_view_window()
                world_view_children = await world_view.children()
                for graphical_spell_window in world_view_children:
                    name = await graphical_spell_window.maybe_read_type_name()
                    if name == 'GraphicalSpellWindow':
                        found = ever_found = True
                        await asyncio.sleep(.05)
                if max-min <= 1:
                    if idx == 0 and not ever_found:
                        return 0
                    return idx+1
                if found:
                    min = idx
                else:
                    max = idx
                idx = int((max-min)/2+min)

    async def get_deck_spell_list(self) -> list[DeckListControlSpellEntry]:
        cards_in_deck_window = await _maybe_get_named_window(self.client.root_window, "CardsInDeck")
        deck_list_control = DynamicDeckListControl(self.client.hook_handler, await cards_in_deck_window.read_base_address())
        list_of_deck_spell_entries = await deck_list_control.spell_entries()
        list_of_valid_deck_spell_entries = []
        deck_count = await self.get_deck_count()
        for idx, entry in enumerate(list_of_deck_spell_entries):
            try:
                graphical = await entry.graphical_spell()
                if not graphical:
                    continue
                template = await graphical.spell_template()
                if not template:
                    continue
                if idx > deck_count-1:
                    break
                list_of_valid_deck_spell_entries.append(entry)
            except MemoryReadError:
                pass
        return list_of_valid_deck_spell_entries

    async def get_item_card_list(self) -> list[_ItemCardSlot]:
        return await self._scan_all_item_pages()

    async def _go_to_item_page(self, page: int) -> None:
        """Navigate ItemSpells to a given 0-indexed page."""
        # The sleeps after each page click stay fixed. The list's start index
        # moves with the page, but BartleBot found a click lands on a newly
        # shown page only some time after that index changes, so it does not say
        # the page is ready.
        # Rewind to page 0 first
        for _ in range(50):
            try:
                prev = await _maybe_get_named_window(self._deck_config_window, "PrevItemSpells")
            except ValueError:
                break
            if await prev.is_control_grayed():
                break
            async with self.client.mouse_handler:
                await self.client.mouse_handler.click_window(prev)
            await asyncio.sleep(0.2)
        # Advance to target page
        for _ in range(page):
            next_btn = await _maybe_get_named_window(self._deck_config_window, "NextItemSpells")
            async with self.client.mouse_handler:
                await self.client.mouse_handler.click_window(next_btn)
            await asyncio.sleep(0.2)

    async def _scan_all_item_pages(self) -> list[_ItemCardSlot]:
        """Hover-scan every item card page and return all slots with name/page/index/active."""
        await self._go_to_item_page(0)
        world_view = await self.client.get_world_view_window()
        results: list[_ItemCardSlot] = []
        page = 0

        while True:
            item_win = await _maybe_get_named_window(self._deck_config_window, "ItemSpells")
            rect = await item_win.scale_to_client()
            slots = self.divide_rectangle(rect, columns=8, rows=2)

            # Children of ItemSpells are overlay sprites on inactive (toggled-off) slots
            sprite_rects: list[Rectangle] = []
            for child in await item_win.children():
                try:
                    sprite_rects.append(await child.scale_to_client())
                except Exception:
                    pass

            async with self.client.mouse_handler:
                for slot_idx, slot_rect in enumerate(slots):
                    await self.client.mouse_handler.set_mouse_position(*slot_rect.center())
                    await asyncio.sleep(0.07)

                    name = None
                    for child in await world_view.children():
                        try:
                            if await child.maybe_read_type_name() != "GraphicalSpellWindow":
                                continue
                            gfx_win = DynamicGraphicalSpellWindow(
                                self.client.hook_handler, await child.read_base_address()
                            )
                            gfx = await gfx_win.graphical_spell()
                            if not gfx:
                                continue
                            tmpl = await gfx.spell_template()
                            if not tmpl:
                                continue
                            n = await tmpl.name()
                            if n:
                                name = n
                            break
                        except Exception:
                            pass

                    if name is None:
                        continue

                    is_active = not any(
                        abs(sr.x1 - slot_rect.x1) < 15 and abs(sr.y1 - slot_rect.y1) < 15
                        for sr in sprite_rects
                    )
                    results.append(_ItemCardSlot(
                        name=name, page=page, slot_index=slot_idx, is_active=is_active
                    ))

            try:
                next_btn = await _maybe_get_named_window(self._deck_config_window, "NextItemSpells")
                if await next_btn.is_control_grayed():
                    break
                async with self.client.mouse_handler:
                    await self.client.mouse_handler.click_window(next_btn)
                await asyncio.sleep(0.3)
                page += 1
            except ValueError:
                break

        return results

    async def get_item_card_count(self) -> int:
        return len(await self._scan_all_item_pages())

    async def get_active_item_card_list(self) -> list[_ItemCardSlot]:
        return [s for s in await self._scan_all_item_pages() if s.is_active]

    async def get_graphical_spell_cards(self) -> list[DynamicGraphicalSpell]:
        # We use this to get a list of DynamicGraphicalSpell which we then pull the names from later on
        list_of_spell_entries = await self.get_spell_list()
        list_of_spell_graphicals = []
        for spell in list_of_spell_entries:
            graphical = await spell.graphical_spell()
            list_of_spell_graphicals.append(graphical)
        return list_of_spell_graphicals

    async def get_graphical_deck_cards(self) -> list[DynamicGraphicalSpell]:
        list_of_deck_spell_entries = await self.get_deck_spell_list()
        list_of_deck_spell_graphicals = []
        for spell in list_of_deck_spell_entries:
            graphical = await spell.graphical_spell()
            try:
                if (not graphical):
                    continue
                template = await graphical.spell_template()
                if (not template):
                    continue
                valid_graphical_spell = await spell.valid_graphical_spell()
                if await template.name() == '':
                    continue
                elif valid_graphical_spell == 0 or valid_graphical_spell == 3:
                    if not await graphical.maybe_read_type_name() == '':
                        list_of_deck_spell_graphicals.append(graphical)
            except Exception:
                pass
        return list_of_deck_spell_graphicals

    async def clear_item_deck(self, *, attempts: int = 5) -> None:
        await self.view_deck_cards()
        # Bounded: a slot whose click never registers used to keep this going forever.
        for _ in range(attempts):
            slots = await self._scan_all_item_pages()
            active = [s for s in slots if s.is_active]
            if not active:
                return
            for page in sorted({s.page for s in active}):
                await self._go_to_item_page(page)
                rect = await self.get_item_spells_rectangle()
                divided = self.divide_rectangle(rect, columns=8, rows=2)
                async with self.client.mouse_handler:
                    for slot in active:
                        if slot.page == page:
                            await self.client.mouse_handler.click(*divided[slot.slot_index].center())
                            await asyncio.sleep(0.1)
        raise RuntimeError(f"Item cards still active after {attempts} passes")

    async def clear_deck(self, *, attempts: int = 3) -> None:
        await self.view_deck_cards()
        try:
            clear_deck_button = await _maybe_get_named_window(self._deck_config_window, "ClearDeckButton")
            async with self.client.mouse_handler:
                await self.client.mouse_handler.click_window(clear_deck_button)

            # The box's windows outlive it in the tree, so finding them says nothing
            # about whether it is up: its caption does
            await _wait_for_message_box(self.client)
            message_box_modal_window = await _maybe_get_named_window(self.client.root_window, "MessageBoxModalWindow")
            leftButton = await _maybe_get_named_window(message_box_modal_window, "leftButton")
            async with self.client.mouse_handler:
                await self.client.mouse_handler.click_window(leftButton)
        except Exception:
            number_of_cards = await self.get_deck_count()
            if number_of_cards == 0:
                return
            await self.clear_deck_manual()
            # We run it again because it might be clicking to fast. It's fast enough that I dont care to run it again
            # Bounded: a deck that never clears used to recurse until the stack ran out.
            if attempts <= 1:
                raise RuntimeError(f"Deck still holds {number_of_cards} card(s) after clearing it")
            await self.clear_deck(attempts=attempts - 1)

    async def clear_deck_manual(self) -> None:
        number_of_cards = await self.get_deck_count()
        if number_of_cards == 0:
            return
        first_card_position = await self.calculate_deck_card_position(1)
        for _ in range(number_of_cards):
            async with self.client.mouse_handler:
                await self.client.mouse_handler.click(*first_card_position)

    async def clear_deck_tcs(self, *, attempts: int = 3) -> None:
        await self.view_tc_cards()
        number_of_cards = await self.get_deck_count()
        if number_of_cards == 0:
            return
        await self.clear_deck_manual()
        # We run it again because it might be clicking to fast. It's fast enough that I dont care to run it again
        # Bounded, as clear_deck is: one that never clears recursed until the stack ran out.
        if attempts <= 1:
            raise RuntimeError(f"Treasure cards still hold {number_of_cards} card(s) after clearing them")
        await self.clear_deck_tcs(attempts=attempts - 1)

    async def clear_full_deck(self):
        await self.clear_deck()
        await self.clear_item_deck()
        # TC clearing is skipped: switching to TC view via clear_deck_tcs() leaves
        # AllPageSpellList in a broken state that prevents normal card restore.
        # TODO: fix view-switch stability so this can be re-enabled:
        # await self.clear_deck_tcs()

    async def _pred_match_template_name(self, coro: Any, template_name: str):
        # I know it works but I still don't understand predicates.
        """
        Args:
            coro: pred function to call
            template_name: The debug name of the cards to find
        Returns: list of possibility with the name
        """

        async def _pred(card):
            graphical = await card.graphical_spell()
            if not graphical:
                return False
            template = await graphical.spell_template()
            if not template:
                return False
            try:
                return template_name == await template.name()
            except MemoryReadError:
                return False

        return await coro(_pred)

    async def spell_list_match_template(self, template_name: str) -> list[SpellListControlSpellEntry]:
        async def spell_list_get_cards_with_predicate(pred: Any) -> list[DynamicGraphicalSpell]:
            """
            Return cards that match a predicate

            Args:
                pred: The predicate function
            """
            cards = []
            spell_list = await self.get_spell_list()
            for spell in spell_list:
                if await pred(spell):
                    cards.append(spell)

            return cards
        return await self._pred_match_template_name(spell_list_get_cards_with_predicate, template_name)

    async def deck_list_match_template(self, template_name: str) -> SpellListControlSpellEntry:
        async def deck_list_get_cards_with_predicate(pred: Any) -> list[DynamicGraphicalSpell]:
            """
            Return cards that match a predicate

            Args:
                pred: The predicate function
            """
            cards = []
            spell_list = await self.get_deck_spell_list()
            for spell in spell_list:
                if await pred(spell):
                    cards.append(spell)

            return cards
        return await self._pred_match_template_name(deck_list_get_cards_with_predicate, template_name)

    async def set_page(self, page_number: int):
        """Show spellbook page ``page_number`` (0-based) by writing the list's start index.

        The game draws the page, and clicks and PageDown behave as if it had been
        paged to.  But it also keeps a second page counter that only PageUp/PageDown
        update, and two things read that one instead:

        - The buttons' enabled state.  After writing a later page, PageUp stays
          greyed and clicking it does nothing.
        - Closing the tier flyout, which restores the page from it -- to page 1
          after a write.

        So never mix set_page with the page buttons, and call it again after
        closing a flyout.
        """
        spell_list_window = await _maybe_get_named_window(self._deck_config_window, "AllPageSpellList")
        spell_list_control = DynamicSpellListControl(self.client.hook_handler, await spell_list_window.read_base_address())
        # Only whole pages within the list have been tested; a start index past the
        # end may be read as an entry.
        page_count = max(1, math.ceil(len(await spell_list_control.spell_entries()) / 6))
        if not 0 <= page_number < page_count:
            raise ValueError(f"page {page_number} is outside the spellbook's {page_count} page(s)")
        await spell_list_control.write_start_index(page_number * 6)

    async def get_spell_list_rectangle(self) -> Rectangle:
        # Returns the size of the window as a rectangle so we can subdivide it later
        self._deck_config_window = await _maybe_get_named_window(self.client.root_window, "DeckConfiguration")
        self.spell_list = await _maybe_get_named_window(self._deck_config_window, "AllPageSpellList")
        self.spell_list_scaled = await self.spell_list.scale_to_client()
        return self.spell_list_scaled

    async def get_deck_list_rectangle(self) -> Rectangle:
        # Returns the size of the window as a rectangle so we can subdivide it later
        self._deck_config_window = await _maybe_get_named_window(self.client.root_window, "DeckConfiguration")
        self.deck_list = await _maybe_get_named_window(self._deck_config_window, "CardsInDeck")
        self.deck_list_scaled = await self.deck_list.scale_to_client()
        return self.deck_list_scaled

    async def get_item_spells_rectangle(self) -> Rectangle:
        # Returns the size of the window as a rectangle so we can subdivide it later
        self._deck_config_window = await _maybe_get_named_window(self.client.root_window, "DeckConfiguration")
        self.deck_list = await _maybe_get_named_window(self._deck_config_window, "ItemSpells")
        self.deck_list_scaled = await self.deck_list.scale_to_client()
        return self.deck_list_scaled

    def divide_rectangle(self, rectangle: Rectangle, columns: int = 2, rows=3) -> list[Rectangle]:
        # This function takes a window element and subdivides it into Columns X Rows
        # We use this to divide up the CardsInDeck and SpellList windows as the
        # cards inside the windows aren't windows themselves, unfortunately.
        width = rectangle.x2 - rectangle.x1
        height = rectangle.y2 - rectangle.y1

        # Calculate the dimensions of each smaller rectangle
        sub_width = width / columns
        sub_height = height / rows

        rectangles = []

        # Generate the smaller rectangles
        for rows in range(rows):
            for column in range(columns):
                sub_x1 = int(rectangle.x1 + column * sub_width)
                sub_y1 = int(rectangle.y1 + rows * sub_height)
                sub_x2 = int(sub_x1 + sub_width)
                sub_y2 = int(sub_y1 + sub_height)

                rectangles.append(Rectangle(sub_x1, sub_y1, sub_x2, sub_y2))

        return rectangles

    async def calculate_card_position(self, card_number) -> tuple[int, int]:
        spell_list_rectangle = await self.get_spell_list_rectangle()
        rectangle_list = self.divide_rectangle(spell_list_rectangle)
        card_rectangle = rectangle_list[card_number - 1]
        return card_rectangle.center()

    async def calculate_deck_card_position(self, card_number) -> tuple[int, int]:
        spell_list_rectangle = await self.get_deck_list_rectangle()
        rectangle_list = self.divide_rectangle(
            spell_list_rectangle, columns=8, rows=8)
        card_rectangle = rectangle_list[card_number - 1]
        return card_rectangle.center()

    def calcuate_position_of_card_in_page(self, cards: list, name: str) -> tuple[int, int]:
        number_of_cards_per_page = 6
        index_of_card = cards.index(name) + 1
        page_index = math.ceil(index_of_card / number_of_cards_per_page) - 1
        index = (index_of_card) % number_of_cards_per_page
        if index == 0:
            index = 6
        return page_index, index

    async def log_user_in_and_out(self):
        await self.client.send_key(Keycode.ESC, 0.1)
        quit_button = await _maybe_get_named_window(self.client.root_window, "QuitButton")
        async with self.client.mouse_handler:
            await self.client.mouse_handler.click_window(quit_button)

        # The sleeps matter: a window lookup never suspends, so without them these
        # loops held the event loop for the whole logout.
        while True:
            try:
                play_button = await _maybe_get_named_window(self.client.root_window, "btnPlay")
                break
            except ValueError:
                await asyncio.sleep(0.25)
        async with self.client.mouse_handler:
            await self.client.mouse_handler.click_window(play_button)
        while True:
            try:
                await _maybe_get_named_window(self.client.root_window, "btnSpellbook")
                break
            except ValueError:
                await asyncio.sleep(0.25)
        logger.info("User has logged out and logged back in")

    async def add_item_cards(self, section: dict) -> None:
        """Activate item card slots from a preset section. Assumes item deck has been cleared."""
        slots = await self._scan_all_item_pages()

        for name in section:
            if not any(s.name == name for s in slots):
                raise Exception(f"Could not find item card: {name}")

        for name, count in section.items():
            named = [s for s in slots if s.name == name]
            for slot in named[:count]:
                await self._go_to_item_page(slot.page)
                rect = await self.get_item_spells_rectangle()
                divided = self.divide_rectangle(rect, columns=8, rows=2)
                async with self.client.mouse_handler:
                    await self.client.mouse_handler.click(*divided[slot.slot_index].center())
                await asyncio.sleep(0.15)

    async def add_item_by_name(self, name: str, number_of_copies: int,
                                slots: list[_ItemCardSlot], sleep: float) -> None:
        named = [s for s in slots if s.name == name]
        active = [s for s in named if s.is_active]
        inactive = [s for s in named if not s.is_active]

        if len(active) > number_of_copies:
            to_deactivate = active[:len(active) - number_of_copies]
        elif len(active) < number_of_copies:
            to_deactivate = []
            to_activate = inactive[:number_of_copies - len(active)]
            for slot in to_activate:
                await self._go_to_item_page(slot.page)
                rect = await self.get_item_spells_rectangle()
                divided = self.divide_rectangle(rect, columns=8, rows=2)
                async with self.client.mouse_handler:
                    await self.client.mouse_handler.click(*divided[slot.slot_index].center())
                    await asyncio.sleep(sleep)
            return
        else:
            return

        for slot in to_deactivate:
            await self._go_to_item_page(slot.page)
            rect = await self.get_item_spells_rectangle()
            divided = self.divide_rectangle(rect, columns=8, rows=2)
            async with self.client.mouse_handler:
                await self.client.mouse_handler.click(*divided[slot.slot_index].center())
                await asyncio.sleep(sleep)

    async def _go_to_spell_page(self, page: int) -> None:
        """Navigate AllPageSpellList to a 0-indexed page using PageUp/PageDown buttons."""
        # The sleeps stay fixed; see _go_to_item_page
        for _ in range(50):
            try:
                prev = await _maybe_get_named_window(self._deck_config_window, "PageUp")
            except ValueError:
                break
            if await prev.is_control_grayed():
                break
            async with self.client.mouse_handler:
                await self.client.mouse_handler.click_window(prev)
            await asyncio.sleep(0.1)
        for _ in range(page):
            next_btn = await _maybe_get_named_window(self._deck_config_window, "PageDown")
            async with self.client.mouse_handler:
                await self.client.mouse_handler.click_window(next_btn)
            await asyncio.sleep(0.1)

    async def _add_all_normal_cards(self, section: dict) -> None:
        """Add all normal deck cards from a preset section.

        Pages with the PageUp/PageDown buttons.  set_page() once crashed the
        client by writing a stale offset (the entry vector's end pointer); that
        is fixed, but it must not be mixed with these button clicks -- see its
        docstring.  Spell positions are precomputed before any page navigation.

        Normal and tiered cards are merged into one queue sorted by page so the
        entire spellbook is traversed in a single forward sweep.
        """
        all_spells = await self.get_graphical_spell_cards()
        spell_names = []
        for spell in all_spells:
            template = await spell.spell_template()
            spell_names.append(await template.name() if template else "")

        # Precompute page + click position for every card before touching pages.
        # Queue entries: (page_idx, "normal", count, position)
        #             or (page_idx, "tiered", name, count, base_position)
        queue = []
        for name, count in section.items():
            if name in spell_names:
                page_idx, card_idx = self.calcuate_position_of_card_in_page(spell_names, name)
                position = await self.calculate_card_position(card_idx)
                queue.append((page_idx, "normal", count, position))
            elif " - T" in name:
                base_name = name[:name.find(" - T")]
                if base_name not in spell_names:
                    raise Exception(f"Base spell '{base_name}' not found for tiered spell '{name}'")
                page_idx, card_idx = self.calcuate_position_of_card_in_page(spell_names, base_name)
                base_position = await self.calculate_card_position(card_idx)
                queue.append((page_idx, "tiered", name, count, base_position))
            else:
                raise Exception(f"Card not found: {name}")

        queue.sort(key=lambda x: x[0])

        # Single forward sweep — rewind once, then only advance
        for _ in range(50):
            try:
                prev = await _maybe_get_named_window(self._deck_config_window, "PageUp")
            except ValueError:
                break
            if await prev.is_control_grayed():
                break
            async with self.client.mouse_handler:
                await self.client.mouse_handler.click_window(prev)
            await asyncio.sleep(0.1)

        current_page = 0
        for entry in queue:
            page_idx = entry[0]
            while current_page < page_idx:
                next_btn = await _maybe_get_named_window(self._deck_config_window, "PageDown")
                async with self.client.mouse_handler:
                    await self.client.mouse_handler.click_window(next_btn)
                await asyncio.sleep(0.1)
                current_page += 1

            if entry[1] == "normal":
                _, _, count, position = entry
                async with self.client.mouse_handler:
                    for _ in range(count):
                        await self.client.mouse_handler.click(*position)
                        await asyncio.sleep(0.05)
            else:
                _, _, name, count, base_position = entry
                # Click base spell to open the tiered variant menu
                async with self.client.mouse_handler:
                    await self.client.mouse_handler.click(*base_position)
                tiered_cards = await self.tiered_spell_list_match_template(name)
                if not tiered_cards:
                    raise Exception(f"Tiered variant '{name}' not found in tiered spell list")
                tiered_spells = await self.get_graphical_tiered_spell_cards()
                tiered_names = []
                for spell in tiered_spells:
                    tmpl = await spell.spell_template()
                    if tmpl:
                        tiered_names.append(await tmpl.name())
                _, tiered_index = self.calcuate_position_of_card_in_page(tiered_names, name)
                tiered_position = await self.calculate_tiered_spell_card_position(tiered_index)
                async with self.client.mouse_handler:
                    for _ in range(count):
                        await self.client.mouse_handler.click(*tiered_position)
                        await asyncio.sleep(0.05)
                # Close tiered menu; spell list stays on current_page
                try:
                    close_btn = await _maybe_get_named_window(
                        self._deck_config_window, "CloseTSMPUnlockedPageButton"
                    )
                    async with self.client.mouse_handler:
                        await self.client.mouse_handler.click_window(close_btn)
                except ValueError:
                    await self.refresh_deck_page()
                    current_page = 0

    async def _add_from_spell_list(self, name: str, number_of_copies: int):
        """Add a card by clicking it in the main SpellList."""
        list_of_spells = await self.get_graphical_spell_cards()
        list_of_spell_names = []
        for spell in list_of_spells:
            template = await spell.spell_template()
            if not template:
                continue
            list_of_spell_names.append(await template.name())
        card_page, card_index_on_page = self.calcuate_position_of_card_in_page(
            list_of_spell_names, name)
        card_position_on_page = await self.calculate_card_position(card_index_on_page)
        await self._go_to_spell_page(card_page)
        async with self.client.mouse_handler:
            for _ in range(number_of_copies):
                await self.client.mouse_handler.click(*card_position_on_page)
                await asyncio.sleep(0.05)

    async def _add_from_tiered_spell_list(self, name: str, number_of_copies: int):
        """Add a tiered spell variant by clicking the base spell to open the
        tiered view, then clicking the specific variant.

        Flow: find base spell in SpellList -> click to open tiered view ->
        find variant in TieredSpellMPUnlockedList -> click the variant.
        """
        # Extract the base spell name (everything before " - T")
        base_name_end = name.find(" - T")
        if base_name_end == -1:
            raise Exception(f"Card not found: {name}")

        base_name = name[:base_name_end]

        # Find and click the base spell in the main spell list to open tiered view
        list_of_spells = await self.get_graphical_spell_cards()
        list_of_spell_names = []
        for spell in list_of_spells:
            template = await spell.spell_template()
            if not template:
                continue
            list_of_spell_names.append(await template.name())

        if base_name not in list_of_spell_names:
            raise Exception(f"Base spell '{base_name}' not found for tiered spell '{name}'")

        card_page, card_index_on_page = self.calcuate_position_of_card_in_page(
            list_of_spell_names, base_name)
        card_position_on_page = await self.calculate_card_position(card_index_on_page)
        await self._go_to_spell_page(card_page)

        # Click the base spell to open the tiered spell list
        async with self.client.mouse_handler:
            await self.client.mouse_handler.click(*card_position_on_page)

        # Find the variant in the tiered spell list
        tiered_cards = await self.tiered_spell_list_match_template(name)
        if len(tiered_cards) <= 0:
            raise Exception(f"Tiered variant '{name}' not found in tiered spell list")

        # Find position and click in the tiered spell list
        tiered_spells = await self.get_graphical_tiered_spell_cards()
        tiered_names = []
        for spell in tiered_spells:
            template = await spell.spell_template()
            if not template:
                continue
            tiered_names.append(await template.name())

        # No page navigation needed: max 3 variants always fit on the first page
        # (slots 3-5 of the 6-slot grid; slots 1-2 are spacers).
        _, tiered_index = self.calcuate_position_of_card_in_page(tiered_names, name)
        tiered_position = await self.calculate_tiered_spell_card_position(tiered_index)
        async with self.client.mouse_handler:
            for _ in range(number_of_copies):
                await self.client.mouse_handler.click(*tiered_position)
                await asyncio.sleep(0.05)

        # Close the tiered spell page and return to the normal spell list
        try:
            close_btn = await _maybe_get_named_window(
                self._deck_config_window, "CloseTSMPUnlockedPageButton"
            )
            async with self.client.mouse_handler:
                await self.client.mouse_handler.click_window(close_btn)
        except ValueError:
            # Button not found, fall back to refreshing the deck page
            await self.refresh_deck_page()

    async def add_by_name(self, name: str, number_of_copies: Optional[int]):
        """
        builder.add_card_by_name("unicorn", number_of_copies: int | None)
        -> number_of_copies = None: add max copies
        -> raises: ValueError(already at max copies)
        -> raises: ValueError(card not found)
        """
        cards: list[SpellListControlSpellEntry] = await self.spell_list_match_template(name)
        is_tiered = len(cards) <= 0

        if is_tiered:
            # Card not in main spell list — check if it's a tiered variant
            # (names containing " - T") behind the TieredSpellMPUnlockedList
            if " - T" not in name:
                raise Exception(f"Card not found: {name}")
            # For tiered spells, we need to open the tiered view to check
            # copies. Just proceed with the requested count.
            if number_of_copies is None or number_of_copies <= 0:
                number_of_copies = 1
            await self._add_from_tiered_spell_list(name, number_of_copies)
        else:
            card = cards[0]

            if number_of_copies is None:
                number_of_copies = (await card.max_copies()) - (await card.current_copies())

            if await card.max_copies() == await card.current_copies():
                raise ValueError(f"already at max copies for {name}")
            elif await card.max_copies() < (await card.current_copies()) + (number_of_copies):
                raise ValueError(
                    f"number of copies is greater than the card allows")

            await self._add_from_spell_list(name, number_of_copies)

    async def remove_by_name(self, name: str, number_of_copies: int):
        desk_list = await self.get_graphical_deck_cards()
        list_of_spell_names = []
        calcuated_copies = 0
        for spell in desk_list:
            template = await spell.spell_template()
            if (not template):
                continue
            list_of_spell_names.append(await template.name())
            if await template.name() == name:
                calcuated_copies = calcuated_copies + 1
        # More than the deck holds is the error. `!=` refused removing one of
        # three copies, and let zero through to a lookup that raised.
        if number_of_copies > calcuated_copies:
            raise ValueError(f"Trying to delete more '{name}' spells than are in the deck")
        if not number_of_copies:
            return
        index = list_of_spell_names.index(name)
        deck_rect = await self.get_deck_list_rectangle()
        divided_deck_rect = self.divide_rectangle(deck_rect, columns=8, rows=8)
        sign_card = divided_deck_rect[index]
        async with self.client.mouse_handler:
            for _ in range(number_of_copies):
                await self.client.mouse_handler.click(*(sign_card.center()))
                await asyncio.sleep(1)

    async def parse_deck_cards(self, tc: bool = False) -> list:  # noqa: ARG002
        # tc parameter is kept for API compatibility but is no longer used for filtering;
        # switch to the correct view (view_deck_cards / view_tc_cards) before calling.
        count = await self.get_deck_count()
        if count == 0:
            return []

        deck_rect = await self.get_deck_list_rectangle()
        slots = self.divide_rectangle(deck_rect, columns=8, rows=8)
        world_view = await self.client.get_world_view_window()
        card_names = []

        async with self.client.mouse_handler:
            for slot_rect in slots[:count]:
                await self.client.mouse_handler.set_mouse_position(*slot_rect.center())
                await asyncio.sleep(0.07)

                name = None
                for child in await world_view.children():
                    try:
                        if await child.maybe_read_type_name() != "GraphicalSpellWindow":
                            continue
                        gfx_win = DynamicGraphicalSpellWindow(
                            self.client.hook_handler, await child.read_base_address()
                        )
                        gfx = await gfx_win.graphical_spell()
                        if not gfx:
                            continue
                        tmpl = await gfx.spell_template()
                        if not tmpl:
                            continue
                        n = await tmpl.name()
                        if n:
                            name = n
                        break
                    except Exception:
                        pass

                if name:
                    card_names.append(name)

        return card_names

    async def get_deck_preset(self) -> dict:
        # get_deck_preset works but objects in memory cause artifacting with treasure cards
        """
        builder.get_deck_preset() -> dict[...]
        {
            normal: {template id: number of copies},
            tc: {template id: number of copies},
            item: {template id: number of copies}
        }
        """
        def dict_maker(_list: list):
            d = {}
            for card in _list:
                if card in d:
                    d[card] = d[card] + 1
                else:
                    d[card] = 1
            return d
        normal_cards = []
        tc_cards = []
        item_cards = []
        assert (self._deck_config_window is not None)
        # Below we are checking if deck window is already open because
        # we can't determine what page the user is on (tc/normal)
        await self.refresh_deck_page()
        normal_cards = await self.parse_deck_cards()
        await self.view_deck_cards()
        item_cards_spell_list = await self.get_active_item_card_list()
        await self.view_tc_cards()
        tc_cards = await self.parse_deck_cards()
        for slot in item_cards_spell_list:
            item_cards.append(slot.name)
        deck = {
            'normal': dict_maker(normal_cards),
            'item': dict_maker(item_cards),
            'tc': dict_maker(tc_cards),
        }
        return deck

    async def set_deck_preset(self, preset: dict):
        await self.refresh_deck_page()
        await self.clear_full_deck()
        deck_section = preset.keys()
        for section in deck_section:
            if section == "normal":
                await self.view_deck_cards()
                await self._add_all_normal_cards(preset[section])
            elif section == "item":
                await self.view_deck_cards()
                await self.add_item_cards(preset[section])
            elif section == "tc":
                pass  # TC restore skipped — view switching breaks the spell list


if __name__ == "__main__":
    pass
