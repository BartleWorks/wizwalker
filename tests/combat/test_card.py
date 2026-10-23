import asyncio

from wizwalker.combat.card import CombatCard
from wizwalker.combat.handler import CombatHandler

from .fakes import Client, Window, planning_phase


class Hand(CombatHandler):
    """A combat handler holding `size` cards, one fewer once `shrink_on` is clicked."""

    def __init__(self, size: int, *, shrink_on=None, root: Window = None):
        super().__init__(Client(root))
        self.size = size
        self.reads = 0
        if shrink_on is not None:
            def on_click(window, right_click):
                if window is shrink_on:
                    self.size -= 1

            self.client.mouse_handler.on_click = on_click

    async def get_cards(self):
        self.reads += 1
        return [object()] * self.size


class Member:
    def __init__(self, health_window):
        self.health_window = health_window

    async def get_health_text_window(self):
        return self.health_window


async def test_an_enchant_selects_hovers_and_clicks_its_target():
    enchant, target = Window("enchant"), Window("target")
    hand = Hand(5, shrink_on=target)
    await CombatCard(hand, enchant).cast(CombatCard(hand, target), sleep_time=0.01)
    assert hand.client.mouse_handler.actions == [("click", enchant), ("hover", target), ("click", target)]
    assert hand.size == 4


async def test_an_enchant_stops_waiting_once_the_hand_shrinks():
    enchant, target = Window("enchant"), Window("target")
    hand = Hand(5, shrink_on=target)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await CombatCard(hand, enchant).cast(CombatCard(hand, target), sleep_time=0.2)
    # three fixed sleeps, and no time spent waiting on a hand that already changed
    assert loop.time() - started < 0.2 * 3 + 0.15


async def test_an_enchant_gives_up_on_a_hand_that_never_changes():
    # A dropped click: the wait is bounded, so a caller that retries gets control back
    enchant, target = Window("enchant"), Window("target")
    hand = Hand(5)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await CombatCard(hand, enchant).cast(CombatCard(hand, target), sleep_time=0.05)
    assert loop.time() - started < 1.0
    assert hand.reads > 2


async def test_a_card_with_no_target_is_one_click():
    card = Window("card")
    hand = Hand(3)
    await CombatCard(hand, card).cast(None, sleep_time=0.01)
    assert hand.client.mouse_handler.actions == [("click", card)]


async def test_a_card_cast_at_a_member_clicks_its_health():
    card, health = Window("card"), Window("Health")
    hand = Hand(3)
    await CombatCard(hand, card).cast(Member(health), sleep_time=0.01)
    assert hand.client.mouse_handler.actions == [("click", card), ("click", health)]


async def test_a_multi_target_card_clicks_each_target_and_confirms():
    confirm = Window("ConfirmTargetsConfirm")
    root = planning_phase(
        sub_window=[Window("ConfirmTargetsWindow", children=[confirm])],
        # a hidden one ahead of it, which a search by name finds first
        decoys=[Window("ConfirmTargetsWindow", visible=False, children=[Window("ConfirmTargetsConfirm")])],
    ).forbid_searches()
    card, first, second = Window("card"), Window("Health"), Window("Health")
    hand = Hand(3, root=root)
    await CombatCard(hand, card).cast([Member(first), Member(second)], sleep_time=0.01)
    assert hand.client.mouse_handler.actions == [
        ("click", card), ("click", first), ("click", second), ("click", confirm)
    ]


async def test_a_multi_target_card_the_path_does_not_describe_is_confirmed_by_name():
    confirm = Window("ConfirmTargetsConfirm")
    root = Window("root", children=[Window("ConfirmTargetsWindow", children=[confirm])])
    card, target = Window("card"), Window("Health")
    hand = Hand(3, root=root)
    await CombatCard(hand, card).cast([Member(target)], sleep_time=0.01)
    assert hand.client.mouse_handler.actions[-1] == ("click", confirm)


async def test_a_multi_target_card_needs_no_confirmation_while_none_is_asked():
    card, target = Window("card"), Window("Health")
    hand = Hand(3, root=planning_phase())
    await CombatCard(hand, card).cast([Member(target)], sleep_time=0.01)
    assert hand.client.mouse_handler.actions == [("click", card), ("click", target)]


async def test_discarding_is_a_right_click_and_waits_for_the_hand():
    card = Window("card")
    hand = Hand(4)
    hand.client.mouse_handler.on_click = lambda window, right: setattr(hand, "size", 3) if right else None
    await CombatCard(hand, card).discard(sleep_time=0.01)
    assert hand.client.mouse_handler.actions == [("right click", card)]
    assert hand.size == 3
