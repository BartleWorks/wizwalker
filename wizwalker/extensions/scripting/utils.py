import asyncio
import regex
from contextlib import suppress

from wizwalker.memory.memory_objects.enums import WindowFlags
from wizwalker.utils import maybe_wait_for_value_with_timeout, poll_until


#: How long a click in the friends list is given to show: a page turned, the
#: list opened, a friend's profile up. Each was a fixed wait or none at all.
FRIENDS_LIST_TIMEOUT = 1.0


_friend_list_entry = regex.compile(
    r"<Y;\d+><X;\d+><indent;0><Color;[\w\d]+><left>"
    r"<icon;FriendsList/Friend_Icon_List_0(?P<icon_list>[12])\."
    r"dds;\d+;\d+;(?P<icon_index>\d+)></left><Y;(?P<name_y>[-\d]+)><X;(?P<name_x>[-\d]+)>"
    r"<indent;\d+><Color;[\d\w]+>(<left>)?<COLOR;[\w\d]+>(?P<name>[\w ]+)"
)


async def paint_window_for(window, time: float = 2):
    """
    Paint a window for a number of seconds

    Args:
        window: The window to paint
        time: How long to paint the window
    """

    async def _paint_task():
        with suppress(asyncio.CancelledError):
            while True:
                await window.debug_paint()

    paint_task = asyncio.create_task(_paint_task())
    await asyncio.sleep(time)
    paint_task.cancel()


async def _maybe_get_named_window(parent, name: str, retries: int = 4):
    async def _try_do(callback, *args, **kwargs):
        while True:
            res = await callback(*args, **kwargs)

            if not res:
                nonlocal retries
                if retries <= 0:
                    return res

                retries -= 1
                await asyncio.sleep(0.4)

            else:
                return res

    possible = await _try_do(parent.get_windows_with_name, name)

    if not possible:
        raise ValueError(f"No child window named {name}")

    if len(possible) > 1:
        raise ValueError(f"Multiple results for {name}")

    return possible[0]


async def _cycle_to_online_friends(client, friends_list):
    list_label = await _maybe_get_named_window(friends_list, "lblFriendsList")

    async def _get_text():
        current_text = await list_label.maybe_text()

        if current_text is None:
            raise Exception("Friend's list has no label")

        return current_text.replace("<center>", "").replace("</center>", "")

    right_button = await _maybe_get_named_window(friends_list, "btnListTypeRight")

    while (current_page := await _get_text()) != "Online Friends":
        await client.mouse_handler.click_window(right_button)
        await maybe_wait_for_value_with_timeout(
            _get_text, value=current_page, inverse_value=True, timeout=5
        )


def _parse_page_number(text: str) -> tuple[int, int]:
    """(current page, page count) from the list's `<center>2 / 3</center>`."""
    current, count = map(
        int, text.replace("<center>", "").replace("</center>", "").replace(" ", "").split("/")
    )
    return current, count


async def _friends_window_of(friends_list):
    """The NewFriendsListWindow a listFriends sits in, or None."""
    with suppress(Exception):
        for parent in await friends_list.get_parents():
            if await parent.name() == "NewFriendsListWindow":
                return parent
    return None


async def _turn_friends_page(client, button, page_number):
    """Click a page button, then wait for the page number to change."""
    before = await page_number.maybe_text() if page_number else None
    await client.mouse_handler.click_window(button)
    if page_number is None:
        return

    async def _turned():
        return await page_number.maybe_text() != before

    await poll_until(_turned, timeout=FRIENDS_LIST_TIMEOUT)


async def _cycle_friends_list(
    client, right_button, friends_list, icon, icon_list, name, current_page
):
    """Find a friend in the list and page to them; returns (entry, index) or (None, index).

    The list's text holds every friend online, ten to a page, whatever page shows.
    """
    list_text = await friends_list.maybe_text()

    match = None
    idx = 0

    for idx, friend_entry in enumerate(list(_friend_list_entry.finditer(list_text))):
        friend_icon = int(friend_entry.group("icon_index"))
        friend_icon_list = int(friend_entry.group("icon_list"))
        friend_name = friend_entry.group("name")

        if icon is not None and icon_list is not None and name:
            if (
                friend_icon == icon
                and friend_icon_list == icon_list
                and friend_name == name
            ):
                match = friend_entry
                break

        elif icon is not None and icon_list is not None:
            if friend_icon == icon and friend_icon_list == icon_list:
                match = friend_entry
                break

        elif name:
            if friend_name == name:
                match = friend_entry
                break

        else:
            raise RuntimeError("Invalid args")

    if match:
        target_page = (idx // 10) + 1

        if target_page != current_page:
            friends_window = await _friends_window_of(friends_list)
            page_number = None
            if friends_window is not None:
                with suppress(ValueError):
                    page_number = await _maybe_get_named_window(friends_window, "PageNumber", retries=0)

            if target_page > current_page:
                button, turns = right_button, target_page - current_page
            else:
                # A list left on a later page used to stay there, and the friend's
                # row was clicked on it: whoever sat there was ported to instead.
                if friends_window is None:
                    raise ValueError("The friend is on an earlier page, and there is no way back to it")
                button = await _maybe_get_named_window(friends_window, "btnArrowUp")
                turns = current_page - target_page

            for _ in range(turns):
                await _turn_friends_page(client, button, page_number)

    return match, idx


async def _click_on_friend(client, friends_list, friend_index):
    scaled_rect = await friends_list.scale_to_client()
    ui_scale = await client.render_context.ui_scale()

    # 12 % 10 = 2 * 30 = 60 * ui_scale
    scaled_friend_name_y = ((friend_index % 10) * 30) * ui_scale

    scaled_rect_center = scaled_rect.center()
    click_x = scaled_rect_center[0]
    # 15 is half the size of each entry's click area
    click_y = int(scaled_rect.y1 + scaled_friend_name_y + (15 * ui_scale))

    # The profile popup stays in the window tree when closed, so what says this
    # click opened it is the popup showing, or showing someone else than before.
    # Up to the second this used to sleep regardless; the full second only when
    # it was already showing this friend.
    popup = await _find_character_window(client)
    before = await _character_window_state(popup)
    await client.mouse_handler.click(click_x, click_y)

    async def _opened():
        nonlocal popup
        # Found once: every search walks the whole window tree
        if popup is None:
            popup = await _find_character_window(client)
        now = await _character_window_state(popup)
        return now is not None and (before is None or now != before)

    await poll_until(_opened, timeout=FRIENDS_LIST_TIMEOUT)


async def _find_character_window(client):
    windows = await client.root_window.get_windows_with_name("wndCharacter")
    return windows[0] if len(windows) == 1 else None


async def _character_window_state(popup):
    """The name the friend profile popup shows, or None while it is closed."""
    if popup is None or not await popup.is_visible():
        return None

    labels = await popup.get_windows_with_name("lblCharacterName")
    # Shown, but nameless, still differs from closed
    return await labels[0].maybe_text() if len(labels) == 1 else ""


async def _message_box_caption(client):
    """The caption of the modal message box, or None while none is up.

    The window tree keeps the box's windows around after it closes, so the window existing
    says nothing; its caption only holds text while a dialog is showing.
    """
    boxes = await client.root_window.get_windows_with_name("MessageBoxModalWindow")
    if len(boxes) != 1:
        return None

    captions = await boxes[0].get_windows_with_name("CaptionText")
    if len(captions) != 1:
        return None

    return await captions[0].maybe_text()


async def _wait_for_message_box(client, timeout: float = 3.0, interval: float = 0.05):
    """Wait for a modal message box to appear, returning as soon as it does.

    Gives up quietly after *timeout*, leaving the caller's own lookup to report a box
    that never came.
    """
    await poll_until(lambda: _message_box_caption(client), timeout=timeout, interval=interval)


async def _teleport_to_friend(client, character_window):
    teleport_button = await _maybe_get_named_window(character_window, "btnGoToFriend")
    await client.mouse_handler.click_window(teleport_button)
    await _wait_for_message_box(client)

    confirmation_window = await _maybe_get_named_window(
        client.root_window, "MessageBoxModalWindow"
    )
    yes_button = await _maybe_get_named_window(confirmation_window, "centerButton")

    close_button = await _maybe_get_named_window(character_window, "btnCharacterClose")

    await client.mouse_handler.click_window(yes_button)
    # we need to click the close button within the 1 second of the teleport animation
    await client.mouse_handler.click_window(close_button)


# TODO: add error if friend is busy message pops up
async def teleport_to_friend_from_list(
    client, *, icon_list: int = None, icon_index: int = None, name: str = None
):
    """
    Teleport to a friend from the client's friend list

    Args:
        client: Client to teleport
        icon_list: Icon list the icon is from (1 or 2) or None
        icon_index: Index of the icon or None
        name: Name of the player or None
    """
    if (
        icon_list is None
        and icon_index is not None
        or icon_list is not None
        and icon_index is None
    ):
        raise ValueError("Icon list and icon index must both be defined or not defined")

    if all(i is None for i in (icon_list, icon_index, name)):
        raise ValueError("Must specify icon_list and icon_index or name or all")

    try:
        friends_window = await _maybe_get_named_window(
            client.root_window, "NewFriendsListWindow"
        )
    except ValueError:
        # friend's list isn't open so open it
        friend_button = await _maybe_get_named_window(client.root_window, "btnFriends")
        await client.mouse_handler.click_window(friend_button)

        friends_window = await _maybe_get_named_window(
            client.root_window, "NewFriendsListWindow"
        )
    else:
        if not await friends_window.is_visible():
            # friend's list isn't open so open it
            friend_button = await _maybe_get_named_window(client.root_window, "btnFriends")
            await client.mouse_handler.click_window(friend_button)
            # It was read straight after the click, before it had opened
            await poll_until(friends_window.is_visible, timeout=FRIENDS_LIST_TIMEOUT)

    await _cycle_to_online_friends(client, friends_window)

    friends_list_window = await _maybe_get_named_window(friends_window, "listFriends")
    friends_list_text = await friends_list_window.maybe_text()

    # no friends online
    if not friends_list_text:
        raise ValueError("No friends online")

    right_button = await _maybe_get_named_window(friends_window, "btnArrowDown")
    page_number = await _maybe_get_named_window(friends_window, "PageNumber")

    current_page, _ = _parse_page_number(await page_number.maybe_text())

    friend, friend_index = await _cycle_friends_list(
        client,
        right_button,
        friends_list_window,
        icon_index,
        icon_list,
        name,
        current_page,
    )

    if friend is None:
        raise ValueError(
            f"Could not find friend with icon {icon_index} icon list {icon_list} and/or name {name}"
        )

    await _click_on_friend(client, friends_list_window, friend_index)

    character_window = await _maybe_get_named_window(client.root_window, "wndCharacter")
    await _teleport_to_friend(client, character_window)

    # close friends window
    await friends_window.write_flags(WindowFlags.disabled)
