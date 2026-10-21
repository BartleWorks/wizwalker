from dataclasses import dataclass, field

import pytest

from wizwalker.memory.memory_objects.conditionals import (
    HangingSpellEffect,
    ReqCombatHealth,
    ReqHangingCharm,
    ReqMinion,
    ReqPipCount,
)
from wizwalker.memory.memory_objects.enums import (
    HangingDisposition,
    MinionType,
    Operator,
    RequirementTarget,
    SpellEffects,
)

REQ = 0x30000


class Effect:
    def __init__(self, kind: SpellEffects, disposition: HangingDisposition):
        self._kind = kind
        self._disposition = disposition

    async def effect_type(self):
        return self._kind

    async def disposition(self):
        return self._disposition


@dataclass
class Participant:
    team: int
    minion: bool = False
    power_pips: int = 0
    hanging: list = field(default_factory=list)

    async def team_id(self):
        return self.team

    async def is_minion(self):
        return self.minion

    async def num_power_pips(self):
        return self.power_pips

    async def hanging_effects(self):
        return self.hanging


@dataclass(eq=False)  # identity, as CombatMember compares
class Member:
    participant: Participant
    hp: int = 100
    max_hp: int = 100

    async def get_participant(self):
        return self.participant

    async def is_minion(self):
        return self.participant.minion

    async def health(self):
        return self.hp

    async def max_health(self):
        return self.max_hp


class Combat:
    def __init__(self, me: Member, *others: Member):
        self.me = me
        self.members = [me, *others]

    async def get_client_member(self):
        return self.me

    async def get_members(self):
        # Fresh objects each call, as CombatHandler builds them
        return [Member(m.participant, m.hp, m.max_hp) for m in self.members]


def requirement(process, hook_handler, req_type, *, target=RequirementTarget.caster, apply_not=False,
                operator=Operator.AND, fields: dict[int, tuple[str, object]] = None):
    process.map(REQ, size=0x80)
    process.put(REQ + 72, "?", apply_not)
    process.put(REQ + 76, "i", operator.value)
    process.put(REQ + 80, "i", target.value)
    for offset, (fmt, value) in (fields or {}).items():
        process.put(REQ + offset, fmt, value)
    return req_type(hook_handler, REQ)


def data(combat, target_idx=0):
    return {"combat": combat, "target_idx": target_idx}


def test_the_hanging_effect_enum_reads_zero():
    # `invalid_spell_effect = 0,` made its value (0,)
    assert HangingSpellEffect(0) is HangingSpellEffect.invalid_spell_effect


@pytest.mark.parametrize(
    "minion_type, minion_team, expected",
    [
        (MinionType.on_team, 1, True),
        (MinionType.on_team, 2, False),
        (MinionType.on_other_team, 2, True),
        (MinionType.on_other_team, 1, False),
        (MinionType.on_any_team, 2, True),
    ],
)
async def test_a_minion_requirement_finds_a_minion_by_team(process, hook_handler, minion_type, minion_team, expected):
    # on_team and on_other_team never held: is_minion() went unawaited behind a
    # membership test no fresh member could pass
    combat = Combat(Member(Participant(team=1)), Member(Participant(team=minion_team, minion=True)))
    req = requirement(process, hook_handler, ReqMinion, fields={88: ("i", minion_type.value)})
    assert await req._evaluate(data(combat)) is expected


@pytest.mark.parametrize("minion_type", [MinionType.on_team, MinionType.on_other_team, MinionType.on_any_team])
async def test_a_minion_requirement_with_no_minion(process, hook_handler, minion_type):
    combat = Combat(Member(Participant(team=1)), Member(Participant(team=2)), Member(Participant(team=1)))
    req = requirement(process, hook_handler, ReqMinion, fields={88: ("i", minion_type.value)})
    assert await req._evaluate(data(combat)) is False


async def test_is_minion_asks_the_target(process, hook_handler):
    combat = Combat(Member(Participant(team=1)), Member(Participant(team=1, minion=True)))
    req = requirement(process, hook_handler, ReqMinion, target=RequirementTarget.target,
                      fields={88: ("i", MinionType.is_minion.value)})
    assert await req._evaluate(data(combat, target_idx=1)) is True
    assert await req._evaluate(data(combat, target_idx=0)) is False


@pytest.mark.parametrize("hp, expected", [(30, True), (60, False), (5, False)])
async def test_combat_health_is_a_share_of_max(process, hook_handler, hp, expected):
    combat = Combat(Member(Participant(team=1), hp=hp, max_hp=100))
    req = requirement(process, hook_handler, ReqCombatHealth, fields={88: ("f", 0.1), 92: ("f", 0.5)})
    assert await req._evaluate(data(combat)) is expected


async def test_pip_count_reads_power_pips(process, hook_handler):
    combat = Combat(Member(Participant(team=1, power_pips=3)))
    req = requirement(process, hook_handler, ReqPipCount, fields={88: ("i", 2), 92: ("i", 4)})
    assert await req._evaluate(data(combat)) is True


async def test_hanging_charms_count_within_the_window(process, hook_handler):
    hanging = [
        Effect(SpellEffects.modify_outgoing_damage, HangingDisposition.beneficial),
        Effect(SpellEffects.modify_outgoing_damage, HangingDisposition.harmful),
        Effect(SpellEffects.damage, HangingDisposition.beneficial),
    ]
    combat = Combat(Member(Participant(team=1, hanging=hanging)))
    req = requirement(process, hook_handler, ReqHangingCharm,
                      fields={88: ("i", HangingDisposition.beneficial.value), 92: ("i", 1), 96: ("i", 1)})
    assert await req._evaluate(data(combat)) is True


@pytest.mark.parametrize(
    "operator, apply_not, before, state, expected",
    [
        (Operator.AND, False, True, True, True),
        (Operator.AND, False, True, False, False),
        (Operator.OR, False, False, True, True),
        (Operator.OR, True, False, True, False),
        (Operator.AND, True, True, False, True),
    ],
)
async def test_requirements_combine_by_their_operator(process, hook_handler, operator, apply_not, before, state, expected):
    req = requirement(process, hook_handler, ReqPipCount, operator=operator, apply_not=apply_not)
    assert await req._do_ops(before, state) is expected
