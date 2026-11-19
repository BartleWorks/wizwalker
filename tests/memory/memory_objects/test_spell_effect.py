from wizwalker.memory.memory_objects.spell_effect import DynamicSpellEffect

EFFECT = 0x40000


async def test_rank_is_m_rank_not_the_shadow_pact_caster_before_it(process, hook_handler):
    # m_shadowPactCasterSC at 208, m_rank at 212
    process.map(EFFECT, size=0x100)
    process.put(EFFECT + 208, "i", 7)
    process.put(EFFECT + 212, "i", 3)
    effect = DynamicSpellEffect(hook_handler, EFFECT)

    assert await effect.rank() == 3

    await effect.write_rank(5)
    assert process.get(EFFECT + 212, "i") == 5
    assert process.get(EFFECT + 208, "i") == 7
