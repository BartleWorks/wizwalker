"""A game UI tree, as far as the code that reads one asks of it."""

from wizwalker.memory.memory_objects.enums import WindowFlags
from wizwalker.utils import Rectangle


class Window:
    def __init__(
        self,
        name: str = "",
        *,
        type_name: str = "Window",
        visible: bool = True,
        text: str = None,
        rect: Rectangle = None,
        grayed: bool = False,
        children=(),
    ):
        self._name = name
        self.type_name = type_name
        self.visible = visible
        self.text = text
        self.rect = rect or Rectangle(0, 0, 10, 10)
        self.grayed = grayed
        self._parent = None
        self._children = []
        for child in children:
            self.add(child)

    def __repr__(self):
        return f"<Window {self._name}>"

    def add(self, child: "Window") -> "Window":
        child._parent = self
        self._children.append(child)
        return child

    def remove(self, child: "Window"):
        self._children.remove(child)
        child._parent = None

    def find(self, name: str) -> "Window":
        """The one window named `name` under this one, for a test to act on."""
        (found,) = [w for w in self._walk() if w._name == name]
        return found

    def _walk(self):
        for child in self._children:
            yield child
            yield from child._walk()

    # --- what Window offers --------------------------------------------------

    async def name(self):
        return self._name

    async def maybe_read_type_name(self):
        return self.type_name

    async def flags(self):
        return WindowFlags.visible if self.visible else WindowFlags(0)

    async def write_flags(self, flags):
        self.visible = WindowFlags.visible in flags

    async def is_visible(self):
        return self.visible

    async def is_control_grayed(self):
        return self.grayed

    async def maybe_text(self):
        return self.text

    async def children(self):
        return list(self._children)

    async def parent(self):
        return self._parent

    async def get_parents(self):
        parents, current = [], self._parent
        while current is not None:
            parents.append(current)
            current = current._parent
        return parents

    async def get_windows_with_name(self, name):
        return [w for w in self._walk() if w._name == name]

    async def get_windows_with_type(self, type_name):
        return [w for w in self._walk() if w.type_name == type_name]

    async def get_child_by_name(self, name):
        for child in self._children:
            if child._name == name:
                return child
        raise ValueError(f"No child named {name}")

    async def scale_to_client(self):
        return self.rect
