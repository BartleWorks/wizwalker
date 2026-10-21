from wizwalker import utils
from wizwalker.client_handler import ClientHandler


class FakeClient:
    def __init__(self, handle):
        self.window_handle = handle
        self.running = True
        self.released = False

    def is_running(self):
        return self.running

    def release_process_handle(self):
        self.released = True


def test_new_clients_are_the_handles_not_yet_seen(monkeypatch):
    handles = [11, 22]
    monkeypatch.setattr(utils, "get_all_wizard_handles", lambda: list(handles))
    handler = ClientHandler(client_cls=FakeClient)

    assert [c.window_handle for c in handler.get_new_clients()] == [11, 22]
    handles.append(33)
    assert [c.window_handle for c in handler.get_new_clients()] == [33]
    assert handler.get_new_clients() == []


def test_dead_clients_are_removed_and_their_handles_released(monkeypatch):
    monkeypatch.setattr(utils, "get_all_wizard_handles", lambda: [11, 22])
    handler = ClientHandler(client_cls=FakeClient)
    alive, dead = handler.get_new_clients()
    dead.running = False

    assert handler.remove_dead_clients() == [dead]
    assert dead.released and not alive.released
    assert handler.clients == [alive]
