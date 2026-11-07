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

CAT_HEAD = (2395, 1325)
CHESTS = {  # registry timer prefix -> screen position, valid once the cat is in position
    "TIME_LEFT_h": (2345, 1327),  # items
    "EMOTE_CHEST_TIME_LEFT_h": (2450, 1327),  # emotes
}
COUNTER_BOX = (2340, 1362, 2420, 1381)  # the toque counter below the cat (left, top, right, bottom)
COUNTER_CHECK_SECONDS = 20  # while clicking, the counter must have changed after this long
WIGGLE_OFFSET = (60, 60)  # where the cursor steps away to when the counter is stuck
CAT_DEFAULT_GRAB = (1286, 692)  # a point on the cat's head right after F1
CAT_DRAG_TO = (2429, 1347)  # where that point must end up for the positions above
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
OCR_SCRIPT =os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr.ps1")
OCR_IMAGE = os.path.join(tempfile.gettempdir(), "bongo_counter.png")  # overwritten on every read
CHEST_COST = 1000  # toques a claimed chest takes off the counter

VK_F1 = 0x70
KEYBOARD_VKS = range(0x08, 0xFF)  # skips the mouse buttons, which we press ourselves
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
GA_ROOT = 2


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
        return ImageGrab.grab(bbox=COUNTER_BOX).tobytes()  # primary monitor only: cheaper
    except OSError:
        return None


def read_counter():
    """The toque counter as a number, read with Windows OCR (~0.5 s), or None."""
    try:
        image = ImageGrab.grab(bbox=COUNTER_BOX).convert("L")
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
    log("cat moved into position")


def main():
    user32.SetProcessDPIAware()
    window = game_window()
    if window:
        place_cat(window)
    else:
        log("Bongo Cat is not open; cat not moved")
    if "--place" in sys.argv:
        return
    log(f"running: clicks after {IDLE_SECONDS}s idle, stops when you touch the PC")
    launch_value = read_counter()
    launch_time = time.monotonic()
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
