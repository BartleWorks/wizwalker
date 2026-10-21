import asyncio

import pytest

from wizwalker import MemoryReadError
from wizwalker.combat import handler as handler_module
from wizwalker.combat.handler import CombatHandler
from wizwalker.memory.memory_objects.enums import DuelPhase

from .fakes import Client, Duel, Window


def spell(name: str, visible: bool = True) -> Window:
    return Window(name, type_name="SpellCheckBox", visible=visible)


async def test_the_hand_reads_left_to_right_and_skips_hidden_cards_and_the_pet():
    a, b, hidden, c = spell("a"), spell("b"), spell("x", visible=False), spell("c")
    root = Window("root", children=[a, b, hidden, spell("PetCard"), c])
    cards = await CombatHandler(Client(root)).get_cards()
    # the game orders them right to left
    assert [card._spell_window for card in cards] == [c, b, a]


async def test_the_card_windows_are_found_once_a_fight():
    root = Window("root", children=[spell("a")])
    handler = CombatHandler(Client(root))
    await handler.get_cards()
    root.add(spell("b"))
    assert len(await handler.get_cards()) == 1


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


async def test_pass_uses_the_defeated_button_while_the_done_window_shows():
    defeated = Window("DefeatedPassButton")
    root = Window("root", children=[Window("DoneWindow", children=[defeated]), Window("Focus")])
    client = Client(root)
    await CombatHandler(client).pass_button()
    assert client.mouse_handler.actions == [("click", defeated)]


async def test_pass_clicks_focus_otherwise():
    focus = Window("Focus")
    root = Window("root", children=[Window("DoneWindow", visible=False), focus])
    client = Client(root)
    await CombatHandler(client).pass_button()
    assert client.mouse_handler.actions == [("click", focus)]


async def test_attempt_cast_reports_a_card_not_in_hand():
    assert await CombatHandler(Client()).attempt_cast("Unicorn") is False
