"""Raises Bongo Cat's counter and claims both chests while you are away.

Run:  python bongo_auto.py          (Ctrl+C or close the window to stop)
Test: python bongo_auto.py --place  (only move the cat into position, then exit)

On start the cat is sent to the game's default spot with F1 and dragged into position.
Simulated input only counts on the cat itself, so it clicks there at the fastest speed
the game registers, after IDLE_SECONDS without you touching the PC. Moving the mouse or
pressing a key stops it at once. Chests are clicked when the game's timers say they are
ready. Clicks only start if the Bongo Cat window is what sits at that point.
Activity goes to bongo_auto.log.
"""
import ctypes
import os
import re
import subprocess
import sys
import tempfile
import time
import winreg
from ctypes import wintypes

from PIL import ImageGrab, ImageOps

# Layout. The game window covers its whole monitor; F1 puts the cat at the window's centre and
# the script drags it to the bottom-right corner, so every point is an offset in physical pixels:
# from the window's centre (right after F1) or from its bottom-right corner (once placed).
# Measured on 2560x1440 with the cat at scale 1 (F1 resets the scale).
GRAB_FROM_CENTER = (6, -28)  # a point on the cat's head right after F1
DRAG_TO_FROM_CORNER = (-131, -93)  # where that point is dragged to
HEAD_FROM_CORNER = (-165, -115)  # where the clicks go
CHESTS_FROM_CORNER = {  # registry timer prefix -> chest, with "Chest position" = 1
    "TIME_LEFT_h": (-215, -113),  # items
    "EMOTE_CHEST_TIME_LEFT_h": (-110, -113),  # emotes
}
COUNTER_FROM_CORNER = (-220, -78, -140, -59)  # the toque counter below the cat (left, top, right, bottom)
VIEW_FROM_CORNER = (-270, -210, -50, -50)  # cat, chests and counter, for the panel's live view

# Screen coordinates, filled in by update_layout() from the game window's position and size.
CAT_HEAD = CAT_DEFAULT_GRAB = CAT_DRAG_TO = (0, 0)
CHESTS = {}
COUNTER_BOX = VIEW_BOX = (0, 0, 0, 0)

COUNTER_CHECK_SECONDS = 20  # while clicking, the counter must have changed after this long
WIGGLE_OFFSET = (60, 60)  # where the cursor steps away to when the counter is stuck
DRAG_STEPS = 40
CLICK_HOLD_SECONDS = 0.035  # measured: ~14 clicks/s all count; 18/s and 22/s lose many
CLICK_GAP_SECONDS = 0.035
IDLE_SECONDS = 15
CHEST_CHECK_SECONDS = 60  # also how often a ready chest is retried
GUARD_CHECK_SECONDS = 1
RETRY_START_SECONDS = 30  # after failing to start clicking, e.g. session locked
WINDOW_TITLE = "BongoCat"
REGISTRY_KEY = r"Software\Irox Games\BongoCat"
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bongo_auto.log")
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3
OCR_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr.ps1")
OCR_IMAGE = os.path.join(tempfile.gettempdir(), "bongo_counter.png")  # overwritten on every read
CHEST_COST = 1000  # toques a claimed chest takes off the counter

VK_F1 = 0x70
KEYBOARD_VKS = range(0x08, 0xFF)  # skips the mouse buttons, which we press ourselves
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
GA_ROOT = 2
MB_ICONWARNING = 0x30


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
kernel32.GetTickCount.restype = wintypes.DWORD
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
SM_CXSCREEN, SM_CYSCREEN = 0, 1


def layout_for(left, top, right, bottom):
    """Screen coordinates of every point the script uses, for a game window with this rect."""
    cx, cy = (left + right) // 2, (top + bottom) // 2

    def corner(dx, dy):
        return right + dx, bottom + dy

    return {
        "grab": (cx + GRAB_FROM_CENTER[0], cy + GRAB_FROM_CENTER[1]),
        "drag_to": corner(*DRAG_TO_FROM_CORNER),
        "head": corner(*HEAD_FROM_CORNER),
        "chests": {prefix: corner(*offset) for prefix, offset in CHESTS_FROM_CORNER.items()},
        "counter": corner(*COUNTER_FROM_CORNER[:2]) + corner(*COUNTER_FROM_CORNER[2:]),
        "view": corner(*VIEW_FROM_CORNER[:2]) + corner(*VIEW_FROM_CORNER[2:]),
    }


def update_layout(window):
    """Point the module's coordinates at the game window as it is now."""
    global CAT_HEAD, CAT_DEFAULT_GRAB, CAT_DRAG_TO, CHESTS, COUNTER_BOX, VIEW_BOX
    rect = wintypes.RECT()
    user32.GetWindowRect(window, ctypes.byref(rect))
    layout = layout_for(rect.left, rect.top, rect.right, rect.bottom)
    CAT_DEFAULT_GRAB, CAT_DRAG_TO, CAT_HEAD = layout["grab"], layout["drag_to"], layout["head"]
    CHESTS, COUNTER_BOX, VIEW_BOX = layout["chests"], layout["counter"], layout["view"]


def is_elevated(pid):
    """True if the process runs as administrator; None if that can't be read."""
    kernel32.OpenProcess.restype = wintypes.HANDLE
    process = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return None
    token = wintypes.HANDLE()
    try:
        if not ctypes.windll.advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return None
        elevation, size = wintypes.DWORD(), wintypes.DWORD()
        ok = ctypes.windll.advapi32.GetTokenInformation(token, 20, ctypes.byref(elevation), 4, ctypes.byref(size))
        return bool(elevation.value) if ok else None
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(process)


def game_runs_as_admin_over_us(window):
    """Windows ignores our cursor moves and clicks on an elevated game unless we are elevated too."""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
    return is_elevated(pid.value) is True and not is_elevated(os.getpid())


def grab(box):
    """Screenshot of a screen box; reads every monitor only when the box is off the primary one."""
    on_primary = (box[0] >= 0 and box[1] >= 0 and box[2] <= user32.GetSystemMetrics(SM_CXSCREEN)
                  and box[3] <= user32.GetSystemMetrics(SM_CYSCREEN))
    return ImageGrab.grab(bbox=box, all_screens=not on_primary)


# Finding the cat on screen. After the drag the cat can sit a few pixels away from the computed
# spot (it does in multiplayer), so the chests and counter are looked up: they are light grey
# boxes, two chests side by side with the counter just below them.
SEARCH_FROM_CORNER = (-300, -250, -20, -45)
MIN_BOX_PIXELS = 150


def is_light(pixel):
    return min(pixel[:3]) >= 215 and max(pixel[:3]) - min(pixel[:3]) <= 18


def light_boxes(image):
    """Bounding boxes (left, top, right, bottom) of the light grey blobs in an RGB image."""
    width, height = image.size
    pixels = image.load()
    seen = bytearray(width * height)
    boxes = []
    for y in range(height):
        for x in range(width):
            if seen[y * width + x] or not is_light(pixels[x, y]):
                continue
            seen[y * width + x] = 1
            stack, count = [(x, y)], 0
            left, top, right, bottom = x, y, x, y
            while stack:
                cx, cy = stack.pop()
                count += 1
                left, top, right, bottom = min(left, cx), min(top, cy), max(right, cx), max(bottom, cy)
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < width and 0 <= ny < height and not seen[ny * width + nx] \
                            and is_light(pixels[nx, ny]):
                        seen[ny * width + nx] = 1
                        stack.append((nx, ny))
            if count >= MIN_BOX_PIXELS:
                boxes.append((left, top, right + 1, bottom + 1))
    return boxes


def cat_from_boxes(boxes):
    """Points of the cat from its two chest boxes and counter box, or None if they aren't there."""
    chests = [b for b in boxes if 28 <= b[2] - b[0] <= 55 and 28 <= b[3] - b[1] <= 60]
    counters = [b for b in boxes if 50 <= b[2] - b[0] <= 130 and 14 <= b[3] - b[1] <= 32]
    for left in chests:
        for right in chests:
            gap = right[0] - left[2]
            if not (40 <= gap <= 100 and abs(right[1] - left[1]) <= 6):
                continue
            items = ((left[0] + left[2]) // 2, (left[1] + left[3]) // 2)
            emotes = ((right[0] + right[2]) // 2, (right[1] + right[3]) // 2)
            chest_bottom = max(left[3], right[3])
            for counter in counters:
                below = counter[1] - chest_bottom
                if 0 <= below <= 20 and left[0] - 10 <= counter[0] <= emotes[0]:
                    return {
                        "head": ((items[0] + emotes[0]) // 2, (items[1] + emotes[1]) // 2),
                        "chests": {"TIME_LEFT_h": items, "EMOTE_CHEST_TIME_LEFT_h": emotes},
                        "counter": (counter[0] + 2, counter[1] + 2, counter[2] - 2, counter[3] - 1),
                    }
    return None


def locate_cat(window):
    """Look the cat up on screen and point the clicks at it. True if it was found."""
    global CAT_HEAD, CHESTS, COUNTER_BOX
    rect = wintypes.RECT()
    user32.GetWindowRect(window, ctypes.byref(rect))
    left, top = rect.right + SEARCH_FROM_CORNER[0], rect.bottom + SEARCH_FROM_CORNER[1]
    try:
        image = grab((left, top, rect.right + SEARCH_FROM_CORNER[2], rect.bottom + SEARCH_FROM_CORNER[3]))
    except OSError:
        return False
    found = cat_from_boxes(light_boxes(image.convert("RGB")))
    if not found:
        return False

    def to_screen(x, y):
        return left + x, top + y

    CAT_HEAD = to_screen(*found["head"])
    CHESTS = {prefix: to_screen(*point) for prefix, point in found["chests"].items()}
    COUNTER_BOX = to_screen(*found["counter"][:2]) + to_screen(*found["counter"][2:])
    return True


def rotate_log():
    """Past LOG_MAX_BYTES, shift bongo_auto.log -> .1 -> .2 ..., keeping LOG_BACKUPS old files."""
    try:
        if os.path.getsize(LOG_PATH) < LOG_MAX_BYTES:
            return
        for n in range(LOG_BACKUPS - 1, 0, -1):
            if os.path.exists(f"{LOG_PATH}.{n}"):
                os.replace(f"{LOG_PATH}.{n}", f"{LOG_PATH}.{n + 1}")
        os.replace(LOG_PATH, f"{LOG_PATH}.1")
    except OSError:  # missing file, or the panel has it open for a moment: try next time
        pass


def log(message):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    rotate_log()
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def game_window():
    return user32.FindWindowW(None, WINDOW_TITLE)


def idle_seconds():
    info = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
    user32.GetLastInputInfo(ctypes.byref(info))
    return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000


def cursor_position():
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def is_key_down():
    return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in KEYBOARD_VKS)


def window_at(point):
    return user32.GetAncestor(user32.WindowFromPoint(wintypes.POINT(*point)), GA_ROOT)


def click(hold=CLICK_HOLD_SECONDS):
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(hold)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def timer_seconds(prefix):
    """Value of the registry timer whose name starts with prefix, or None."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
        index = 0
        while True:
            try:
                name, value, _ = winreg.EnumValue(key, index)
            except OSError:
                return None
            if name.startswith(prefix):
                return value
            index += 1


def counter_snapshot():
    """Pixels of the toque counter, or None if the screen can't be read (e.g. locked)."""
    try:
        return grab(COUNTER_BOX).tobytes()
    except OSError:
        return None


def read_counter():
    """The toque counter as a number, read with Windows OCR (~0.5 s), or None."""
    try:
        image = grab(COUNTER_BOX).convert("L")
        image = ImageOps.autocontrast(image).resize((image.width * 4, image.height * 4))
        ImageOps.expand(image, border=20, fill=image.getpixel((0, 0))).save(OCR_IMAGE)
        text = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", OCR_SCRIPT, OCR_IMAGE],
            capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None


def fmt_count(value):
    return "?" if value is None else f"{value:,}".replace(",", ".")


def fmt_duration(seconds):
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h {rest // 60:02d}m" if hours else f"{rest // 60}m {rest % 60:02d}s"


def check_chests(window):
    """Claim any ready chest, putting the cursor and focus back afterwards. Returns clicks sent."""
    sent = 0
    for prefix, point in CHESTS.items():
        seconds = timer_seconds(prefix)
        if seconds is None or seconds > 0:
            continue
        original = cursor_position()
        foreground = user32.GetForegroundWindow()
        user32.SetCursorPos(*point)
        time.sleep(0.2)  # the overlay only accepts the mouse once the cursor is over it
        if window_at(point) == window:
            click(hold=0.05)
            time.sleep(0.5)
            if (timer_seconds(prefix) or 0) > 0:  # the timer restarts only on a real claim
                sent += 1
                log(f"{prefix.rstrip('_h')} ready: click sent, chest claimed")
            else:
                log(f"{prefix.rstrip('_h')} ready: click sent, not claimed (not enough toques?)")
        else:
            log(f"{prefix.rstrip('_h')} ready: another window on top, skipped")
        time.sleep(0.1)
        user32.SetCursorPos(*original)
        if foreground:
            user32.SetForegroundWindow(foreground)
    return sent


def place_cat(window):
    """Reset the cat with F1 and drag it into position, restoring cursor and focus."""
    update_layout(window)  # the game may have been restarted or moved to another monitor
    original = cursor_position()
    foreground = user32.GetForegroundWindow()
    user32.SetForegroundWindow(window)
    time.sleep(0.3)
    if user32.GetForegroundWindow() != window:
        log("could not focus Bongo Cat; cat not moved")
        return
    user32.keybd_event(VK_F1, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_F1, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(1.0)

    start_x, start_y = CAT_DEFAULT_GRAB
    end_x, end_y = CAT_DRAG_TO
    user32.SetCursorPos(start_x, start_y)
    time.sleep(0.3)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.15)
    for step in range(1, DRAG_STEPS + 1):
        user32.SetCursorPos(
            start_x + (end_x - start_x) * step // DRAG_STEPS,
            start_y + (end_y - start_y) * step // DRAG_STEPS,
        )
        time.sleep(0.02)
    time.sleep(0.15)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.2)
    user32.SetCursorPos(*original)
    if foreground:
        user32.SetForegroundWindow(foreground)
    time.sleep(0.3)  # let the chests and counter settle before looking for them
    if locate_cat(window):
        log(f"cat moved into position, found at {CAT_HEAD}")
    else:
        log("cat moved into position, chests and counter not found on screen: using computed positions")


def main():
    user32.SetProcessDPIAware()
    window = game_window()
    if window and game_runs_as_admin_over_us(window):
        log("Bongo Cat runs as administrator: Windows blocks our clicks on it. Not starting. "
            "Untick 'Run this program as an administrator' in BongoCat.exe properties and restart the game")
        user32.MessageBoxW(None, "Bongo Cat is running as administrator, so Windows blocks the farmer's "
                           "clicks.\n\nIn BongoCat.exe > Properties > Compatibility, untick 'Run this program "
                           "as an administrator', restart the game and run Start.bat again.",
                           "Bongo Auto", MB_ICONWARNING)
        sys.exit(1)
    if window:
        place_cat(window)
    else:
        log("Bongo Cat is not open; cat not moved")
    if "--place" in sys.argv:
        return
    log(f"running: clicks after {IDLE_SECONDS}s idle, stops when you touch the PC")
    launch_value = read_counter() if window else None
    launch_time = time.monotonic()
    if window and launch_value is None:
        log(f"counter not found at {COUNTER_BOX}: the cat's layout may not match this screen "
            "(see Adjusting positions in README.md) or the counter is hidden")
    else:
        log(f"counter at launch: {fmt_count(launch_value)}")
    total_gain = 0  # toques earned while clicking, chests added back
    total_claims = 0
    session_value = None
    session_start = 0.0
    session_claims = 0

    def end_session(reason):
        nonlocal total_gain
        end_value = read_counter()
        seconds = time.monotonic() - session_start
        if end_value is None or session_value is None:
            log(f"{reason} | session: counter unreadable, {fmt_duration(seconds)}")
            return
        gain = end_value - session_value + session_claims * CHEST_COST
        total_gain += gain
        rate = gain / seconds if seconds else 0
        log(
            f"{reason} | session: {fmt_count(session_value)} -> {fmt_count(end_value)}, "
            f"+{fmt_count(gain)} toques in {fmt_duration(seconds)} ({rate:.1f}/s), "
            f"{session_claims} chests | since launch: +{fmt_count(total_gain)} toques, "
            f"{total_claims} chests in {fmt_duration(time.monotonic() - launch_time)}"
        )

    clicking = False
    user_foreground = None
    last_chest_check = 0.0
    last_guard_check = 0.0
    next_start_attempt = 0.0
    last_counter = None
    last_counter_check = 0.0
    stuck_count = 0
    while True:
        window = game_window()
        now = time.monotonic()
        if not window:
            clicking = False
            time.sleep(1)
            continue

        # Chests wait until you are away, so the cursor never jumps while you use the PC.
        if now - last_chest_check >= CHEST_CHECK_SECONDS and (clicking or idle_seconds() >= IDLE_SECONDS):
            last_chest_check = now
            claimed = check_chests(window)
            total_claims += claimed
            if clicking:
                session_claims += claimed

        if clicking:
            stop_reason = None
            if cursor_position() != CAT_HEAD or is_key_down():
                stop_reason = "you are back: clicks paused"
            elif now - last_guard_check >= GUARD_CHECK_SECONDS:
                last_guard_check = now
                if window_at(CAT_HEAD) != window:
                    stop_reason = "another window on top: clicks paused"
            if stop_reason:
                clicking = False
                if user_foreground:
                    user32.SetForegroundWindow(user_foreground)
                end_session(stop_reason)
                continue
            if now - last_counter_check >= COUNTER_CHECK_SECONDS:
                last_counter_check = now
                snapshot = counter_snapshot()
                if snapshot is not None and last_counter is not None:
                    if snapshot != last_counter:
                        if stuck_count:
                            log("counter rising again")
                        stuck_count = 0
                    else:
                        stuck_count += 1
                        if stuck_count == 1:
                            log("counter stuck: moving the mouse away and back")
                            user32.SetCursorPos(CAT_HEAD[0] + WIGGLE_OFFSET[0], CAT_HEAD[1] + WIGGLE_OFFSET[1])
                            time.sleep(0.3)
                            user32.SetCursorPos(*CAT_HEAD)
                            time.sleep(0.3)
                        else:
                            log("counter still stuck: putting the cat back in position")
                            place_cat(window)
                            stuck_count = 0
                        snapshot = counter_snapshot()
                        last_counter_check = time.monotonic()
                last_counter = snapshot
                continue  # re-run the "you are back" check before clicking again
            click()
            time.sleep(CLICK_GAP_SECONDS)
        elif idle_seconds() >= IDLE_SECONDS and now >= next_start_attempt:
            user_foreground = user32.GetForegroundWindow()
            # Moving the mouse while a click is held drags the cat, which happens whenever
            # you come back mid-click, so put it back in position before every session.
            place_cat(window)
            user32.SetCursorPos(*CAT_HEAD)
            time.sleep(0.2)
            if cursor_position() == CAT_HEAD and window_at(CAT_HEAD) == window:
                clicking = True
                last_counter = counter_snapshot()
                last_counter_check = time.monotonic()
                stuck_count = 0
                session_value = read_counter()
                session_start = time.monotonic()
                session_claims = 0
                log(f"idle: clicking the cat | counter {fmt_count(session_value)}")
            else:
                next_start_attempt = now + RETRY_START_SECONDS
        else:
            time.sleep(0.25)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped")
