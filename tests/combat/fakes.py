"""A client in combat, from the outside: windows, a mouse that records, a duel."""

from wizwalker.memory.memory_objects.enums import DuelPhase

from ..fake_windows import Window


class Mouse:
    """Records every click; `on_click` lets a test make the game answer one."""

    def __init__(self, root: Window):
        self.root = root
        self.actions = []
        self.on_click = None

    async def click_window(self, window, right_click: bool = False, **kwargs):
        self.actions.append(("right click" if right_click else "click", window))
        if self.on_click:
            self.on_click(window, right_click)

    async def set_mouse_position_to_window(self, window, **kwargs):
        self.actions.append(("hover", window))

    async def click_window_with_name(self, name, **kwargs):
        found = await self.root.get_windows_with_name(name)
        if len(found) != 1:
            raise ValueError(name)
        await self.click_window(found[0], **kwargs)


class Duel:
    """Answers the phase and round from a script, one read each, then holds the last."""

    def __init__(self, phases=(DuelPhase.planning,), rounds=(1,)):
        self.phases = list(phases)
        self.rounds = list(rounds)

    @staticmethod
    def _next(script):
        return script.pop(0) if len(script) > 1 else script[0]

    async def duel_phase(self):
        phase = self._next(self.phases)
        if isinstance(phase, BaseException):
            raise phase
        return phase

    async def round_num(self):
        return self._next(self.rounds)


class Client:
    def __init__(self, root: Window = None, duel: Duel = None):
        self.root_window = root or Window("root")
        self.mouse_handler = Mouse(self.root_window)
        self.duel = duel or Duel()

    async def in_battle(self):
        try:
            return await self.duel.duel_phase() is not DuelPhase.ended
        except Exception:
            return False
