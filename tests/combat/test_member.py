import pytest

from wizwalker import utils
from wizwalker.combat.member import CombatMember


class Control:
    """A CombatantControl whose Health child shows up after some searches."""

    def __init__(self, appears_after: int):
        self.appears_after = appears_after
        self.searches = 0

    async def get_windows_with_name(self, name):
        assert name == "Health"
        self.searches += 1
        return ["health window"] if self.searches > self.appears_after else []


async def test_waits_for_the_health_window_to_appear():
    # [] is not None, so the wait returned the empty list at once and never waited
    control = Control(appears_after=2)
    assert await CombatMember(None, control).get_health_text_window() == "health window"
    assert control.searches == 3


async def test_a_health_window_that_never_appears_is_a_value_error(monkeypatch):
    real_wait = utils.maybe_wait_for_any_value_with_timeout

    async def short_wait(coro, sleep_time=0.5, *, timeout=None, ignore_exceptions=True):
        return await real_wait(coro, 0.01, timeout=0.05, ignore_exceptions=ignore_exceptions)

    monkeypatch.setattr(utils, "maybe_wait_for_any_value_with_timeout", short_wait)
    with pytest.raises(ValueError):
        await CombatMember(None, Control(appears_after=10**6)).get_health_text_window()
