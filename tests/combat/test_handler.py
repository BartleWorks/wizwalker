import asyncio

import pytest

from wizwalker import MemoryReadError
from wizwalker.combat import handler as handler_module
from wizwalker.combat.handler import CombatHandler
from wizwalker.memory.memory_objects.enums import DuelPhase

from .fakes import Client, Duel, Window, planning_phase


def spell(name: str, visible: bool = True) -> Window:
    return Window(name, type_name="SpellCheckBox", visible=visible)


def hand(held: int) -> list:
    """`Card1` to `Card7`, the first `held` of them showing, and the pet's card."""
    return [spell(f"Card{i}", visible=i <= held) for i in range(1, 8)] + [spell("PetCard")]


async def test_the_hand_reads_left_to_right_and_skips_hidden_cards_and_the_pet():
    cards = hand(3)
    root = planning_phase(hand=cards, decoys=[spell("Card9")]).forbid_searches()
    read = await CombatHandler(Client(root)).get_cards()
    # the game numbers them right to left
    assert [card._spell_window for card in read] == [cards[2], cards[1], cards[0]]


async def test_a_hand_the_path_does_not_describe_is_found_by_type():
    a, b, hidden, c = spell("a"), spell("b"), spell("x", visible=False), spell("c")
    root = Window("root", children=[a, b, hidden, spell("PetCard"), c])
    cards = await CombatHandler(Client(root)).get_cards()
    assert [card._spell_window for card in cards] == [c, b, a]


async def test_the_card_windows_are_found_once_a_fight():
    root = Window("root", children=[spell("a")])
    handler = CombatHandler(Client(root))
    await handler.get_cards()
    root.add(spell("b"))
    assert len(await handler.get_cards()) == 1


async def test_a_fight_finds_its_windows_afresh():
    old = planning_phase(hand=hand(2))
    client = Client(old)
    handler = CombatHandler(client)
    await handler.get_cards()
    client.root_window = planning_phase(hand=hand(5)).forbid_searches()
    client.duel.phases = [DuelPhase.ended]

    await asyncio.wait_for(handler.handle_combat(), 2)
    assert len(await handler.get_cards()) == 5


async def test_the_combatants_are_read_by_their_path():
    first, second = Window("CombatantControl"), Window("CombatantControl")
    root = planning_phase(combatants=[first, second], decoys=[Window("CombatantControl")]).forbid_searches()
    members = await CombatHandler(Client(root)).get_members()
    assert [member._combatant_control for member in members] == [first, second]


async def test_the_combatants_are_read_again_each_time():
    # They are rebuilt every planning phase, so only their parent may be held
    root = planning_phase(combatants=[Window("CombatantControl")])
    handler = CombatHandler(Client(root))
    await handler.get_members()
    root.find("CombatantControl")._parent.add(Window("CombatantControl"))
    assert len(await handler.get_members()) == 2


async def test_a_planning_phase_that_has_gone_is_found_again():
    root = planning_phase(combatants=[Window("CombatantControl")])
    handler = CombatHandler(Client(root))
    await handler.get_members()
    world_view, gone = root.find("WorldView"), root.find("PlanningPhase")
    world_view.remove(gone)
    gone._children = []  # Window.children() reads a freed window as having none
    newer = planning_phase(combatants=[Window("CombatantControl"), Window("CombatantControl")])
    world_view.add(newer.find("PlanningPhase"))
    root.forbid_searches()
    assert len(await handler.get_members()) == 2


async def test_combatants_the_path_does_not_describe_are_found_by_name():
    root = Window("root", children=[Window("CombatantControl")])
    assert len(await CombatHandler(Client(root)).get_members()) == 1


@pytest.mark.parametrize("phase", [DuelPhase.planning, DuelPhase.ended])
async def test_waiting_for_planning_ends_at_planning_or_the_end(phase):
    duel = Duel(phases=[DuelPhase.execution, DuelPhase.execution, phase])
    await asyncio.wait_for(CombatHandler(Client(duel=duel)).wait_for_planning_phase(sleep_time=0), 2)
    assert duel.phases == [phase]


async def test_waiting_for_planning_ends_on_an_unreadable_duel():
    duel = Duel(phases=[DuelPhase.execution, MemoryReadError(0)])
    await asyncio.wait_for(CombatHandler(Client(duel=duel)).wait_for_planning_phase(sleep_time=0), 2)


async def test_waiting_for_the_next_round_ends_on_a_new_round():
    duel = Duel(phases=[DuelPhase.execution], rounds=[3, 3, 4])
    await asyncio.wait_for(CombatHandler(Client(duel=duel)).wait_until_next_round(3, sleep_time=0), 2)
    assert duel.rounds == [4]


async def test_waiting_for_the_next_round_ends_when_the_fight_does():
    duel = Duel(phases=[DuelPhase.execution, DuelPhase.ended], rounds=[3])
    await asyncio.wait_for(CombatHandler(Client(duel=duel)).wait_until_next_round(3, sleep_time=0), 2)


def test_the_round_waits_read_more_often_than_half_a_second():
    assert handler_module.ROUND_POLL_INTERVAL < 0.5


async def test_a_fight_plays_each_round_once():
    rounds_played = []
    duel = Duel(phases=[DuelPhase.planning], rounds=[1])

    class Handler(CombatHandler):
        async def handle_round(self):
            played = await self.round_number()
            rounds_played.append(played)
            # the game moves on once the round is played, and the fight ends after two
            duel.rounds = [played + 1]
            if played == 2:
                duel.phases = [DuelPhase.ended]

    await asyncio.wait_for(Handler(Client(duel=duel)).handle_combat(), 5)
    assert rounds_played == [1, 2]


class Member:
    def __init__(self, is_client):
        self._is_client = is_client

    async def is_client(self):
        return self._is_client


async def test_the_client_member_is_retried_then_refused(monkeypatch):
    handler = CombatHandler(Client())
    calls = []

    async def get_members():
        calls.append(1)
        return [Member(False)]

    monkeypatch.setattr(handler, "get_members", get_members)
    with pytest.raises(ValueError):
        await handler.get_client_member(retries=3, sleep_time=0)
    assert len(calls) == 3


def action_buttons():
    return [Window("Focus"), Window("Draw"), Window("Flee")]


def done_decoy():
    """A hidden DoneWindow ahead of the real one: the first a search by name finds."""
    return Window("DoneWindow", visible=False, children=[Window("DefeatedPassButton")])


async def test_pass_uses_the_defeated_button_while_the_done_window_shows():
    defeated = Window("DefeatedPassButton")
    root = planning_phase(
        sub_window=[Window("DoneWindow", children=[defeated])],
        action_buttons=action_buttons(),
        decoys=[done_decoy()],
    ).forbid_searches()
    client = Client(root)
    await CombatHandler(client).pass_button()
    assert client.mouse_handler.actions == [("click", defeated)]


async def test_pass_clicks_focus_otherwise():
    buttons = action_buttons()
    root = planning_phase(
        sub_window=[Window("DoneWindow", visible=False)],
        action_buttons=buttons,
        # a click by name refuses a name two windows share
        decoys=[Window("Focus")],
    ).forbid_searches()
    client = Client(root)
    await CombatHandler(client).pass_button()
    assert client.mouse_handler.actions == [("click", buttons[0])]


async def test_a_pass_button_the_path_does_not_describe_is_clicked_by_name():
    focus = Window("Focus")
    root = Window("root", children=[Window("DoneWindow", visible=False), focus])
    client = Client(root)
    await CombatHandler(client).pass_button()
    assert client.mouse_handler.actions == [("click", focus)]


@pytest.mark.parametrize("press, index", [("draw_button", 1), ("flee_button", 2)])
async def test_draw_and_flee_click_their_own_buttons(press, index):
    buttons = action_buttons()
    root = planning_phase(
        sub_window=[Window("DoneWindow", visible=False)],
        action_buttons=buttons,
        decoys=[Window("Draw"), Window("Flee")],
    ).forbid_searches()
    client = Client(root)
    await getattr(CombatHandler(client), press)()
    assert client.mouse_handler.actions == [("click", buttons[index])]


async def test_the_action_buttons_are_found_by_their_path():
    buttons = action_buttons()
    root = planning_phase(action_buttons=buttons, decoys=[Window("Draw")]).forbid_searches()
    assert await CombatHandler(Client(root)).get_action_button("Draw") is buttons[1]


async def test_attempt_cast_reports_a_card_not_in_hand():
    assert await CombatHandler(Client()).attempt_cast("Unicorn") is False


async def test_a_willcast_is_the_pet_card_in_the_hand():
    cards = hand(7)
    root = planning_phase(hand=cards, decoys=[spell("PetCard")]).forbid_searches()
    client = Client(root)
    assert await CombatHandler(client).attempt_willcast() is True
    assert client.mouse_handler.actions == [("click", cards[-1])]


async def test_a_willcast_with_no_pet_card_reports_it():
    # The search this replaced indexed an empty list, and raised IndexError
    assert await CombatHandler(Client(planning_phase(hand=hand(7)[:-1]))).attempt_willcast() is False
    assert await CombatHandler(Client()).attempt_willcast() is False
