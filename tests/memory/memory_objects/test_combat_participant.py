from wizwalker.memory.memory_objects.combat_participant import DynamicCombatParticipant

PARTICIPANT = 0x40000


async def test_is_monster_reads_m_is_monster_not_the_max_health_before_it(process, hook_handler):
    # r814353's layout: m_curMaxHP at 420, m_minionStartingHealth at 424, m_isMonster at 428
    process.map(PARTICIPANT, size=0x400)
    process.put(PARTICIPANT + 420, "i", 4250)
    process.put(PARTICIPANT + 424, "i", 900)
    process.put(PARTICIPANT + 428, "I", 1)

    assert await DynamicCombatParticipant(hook_handler, PARTICIPANT).is_monster() == 1
