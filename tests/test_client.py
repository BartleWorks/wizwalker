from types import SimpleNamespace

import pytest

from wizwalker.client import Client


class Zones:
    """A client whose zone and loading screen follow a script, one read each."""

    def __init__(self, zones, loading):
        self.zones = list(zones)
        self.loading = list(loading)

    async def zone_name(self):
        return self.zones.pop(0) if len(self.zones) > 1 else self.zones[0]

    async def is_loading(self):
        return self.loading.pop(0) if self.loading else False


@pytest.mark.parametrize("sleep_time", [None, 0, 0.01])
async def test_wait_for_zone_change_waits_out_the_zone_and_the_load(sleep_time):
    # sleep_time=None is documented as no pause, and raised TypeError from asyncio.sleep
    client = Zones(["A", "A", "A", "B"], [True, True, False])
    await Client.wait_for_zone_change(client, sleep_time=sleep_time)
    assert client.zones == ["B"]
    assert client.loading == []


async def test_wait_for_zone_change_from_a_named_zone():
    client = Zones(["B"], [])
    await Client.wait_for_zone_change(client, "A", sleep_time=None)


async def test_in_battle_is_false_when_the_phase_cannot_be_read():
    from wizwalker import MemoryReadError

    class Duel:
        async def duel_phase(self):
            raise MemoryReadError(0)

    assert await Client.in_battle(SimpleNamespace(duel=Duel())) is False
