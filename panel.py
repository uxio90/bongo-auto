"""Local web panel for the Bongo Cat farm: script state, chest timers, live counter
and the Steam inventory with market prices.

Run:  python panel.py      then open http://localhost:8765
Reads the script's log, the game's registry timers, a screen crop of the cat and public
Steam pages (inventory of the last account signed in to Steam every 10 min, the game's
whole market every 30 min). Writes only: new drops to the log, prices.json, known_assets.json.
The page itself is panel.html, read on every request.
"""
import ctypes
import io
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
import winreg
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bongo_auto  # shares the cat layout, so the live view follows the game window

PORT = 8765
HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "bongo_auto.log")
REGISTRY_KEY = r"Software\Irox Games\BongoCat"
CHESTS = {"TIME_LEFT_h": "Items", "EMOTE_CHEST_TIME_LEFT_h": "Emotes"}
APP_ID = 3419430
INVENTORY_PAGE = 2000  # Steam's maximum per request; more_items/last_assetid give the next page
INVENTORY_PAGE_GAP_SECONDS = 3
INVENTORY_URL = "https://steamcommunity.com/inventory/{steam_id}/{app}/2?l=english&count=%d" % INVENTORY_PAGE
CHEST_PERIOD_SECONDS = 1800
MARKET_URL = (
    "https://steamcommunity.com/market/search/render/?appid={app}&norender=1"
    "&count=100&start={start}&sort_column=name&sort_dir=asc"
)
ICON_URL = "https://community.cloudflare.steamstatic.com/economy/image/{}/96fx96f"
INVENTORY_REFRESH_SECONDS = 600
INVENTORY_RETRY_SECONDS = 600  # retrying sooner keeps Steam's 429 block alive
INVENTORY_PATH = os.path.join(HERE, "inventory.json")  # last good inventory, shown after a restart
MARKET_REFRESH_SECONDS = 1800  # the whole market of the game, ~6 requests
MARKET_PAGE_GAP_SECONDS = 5
MARKET_RETRY_SECONDS = 600  # retrying sooner keeps Steam's 429 block alive
PRICES_PATH = os.path.join(HERE, "prices.json")  # survives panel restarts
KNOWN_ASSETS_PATH = os.path.join(HERE, "known_assets.json")  # to spot newly dropped items
SCRIPT_CHECK_SECONDS = 15
LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (.*)$")

ctypes.windll.user32.SetProcessDPIAware()

state_lock = threading.Lock()
inventory = {"items": [], "updated": None, "error": None, "account": None}
prices = {"items": {}, "updated": None, "error": None}  # items: hash_name -> {"usd", "listings"}
script_alive = {"value": None, "at": 0.0}


def steam_account():
    """(steam_id, account name, persona name) of the last account signed in to Steam on this PC."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
        steam_path = winreg.QueryValueEx(key, "SteamPath")[0]
    with open(os.path.join(steam_path, "config", "loginusers.vdf"), encoding="utf-8") as f:
        text = f.read()
    accounts = []
    for steam_id, body in re.findall(r'"(\d{17})"\s*\{(.*?)\}', text, re.S):
        fields = dict(re.findall(r'"(\w+)"\s+"([^"]*)"', body))
        accounts.append((fields.get("MostRecent") == "1", steam_id, fields))
    if not accounts:
        raise RuntimeError("no Steam account found in loginusers.vdf")
    _, steam_id, fields = max(accounts, key=lambda a: a[0])
    return steam_id, fields.get("AccountName", ""), fields.get("PersonaName", "")


def http_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "BongoPanel/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def timers():
    result = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            index = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                for prefix, label in CHESTS.items():
                    if name.startswith(prefix):
                        result[label] = value
                index += 1
    except OSError:
        pass
    return result


def is_script_running():
    now = time.monotonic()
    if script_alive["value"] is None or now - script_alive["at"] >= SCRIPT_CHECK_SECONDS:
        command = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "Where-Object { $_.CommandLine -match 'bongo_auto' } | Measure-Object | "
            "Select-Object -ExpandProperty Count"
        )
        try:
            output = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True, text=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout.strip()
            script_alive["value"] = output.isdigit() and int(output) > 0
        except (OSError, subprocess.SubprocessError):
            script_alive["value"] = None
        script_alive["at"] = now
    return script_alive["value"]


def read_log():
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        match = LOG_LINE.match(line)
        if match:
            entries.append((datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S"), match[2]))
    return entries


def script_state(entries):
    """What the script is doing, from its latest state-changing log line."""
    for when, message in reversed(entries):
        if message.startswith(("counter stuck", "counter still stuck")):
            return "stuck", "Counter not rising: moving the mouse/cat", when
        if message.startswith(("idle: clicking", "counter rising again")):
            return "clicking", "Clicking the cat", when
        if message.startswith("you are back"):
            return "paused", "Paused: you are using the PC", when
        if message.startswith("another window on top"):
            return "paused", "Paused: another window covers the cat", when
        if message.startswith(("running", "stopped")):
            return "waiting", "Waiting for 15 s without activity", when
    return "unknown", "No data in the log", None


def claims_today(entries):
    """Chest claims today; a click repeated every minute for the same chest is one attempt."""
    today = datetime.now().date()
    last_click = {}
    claims = []
    for when, message in entries:
        # "click sent, chest claimed" now; plain "click sent" in logs from older versions
        match = re.match(r"(\w+) ready: click sent(, chest claimed)?$", message)
        if not match or when.date() != today:
            continue
        chest = "Emotes" if match[1].startswith("EMOTE") else "Items"
        previous = last_click.get(chest)
        if previous and (when - previous).total_seconds() < 300:
            claims[-1 if claims[-1]["chest"] == chest else -2]["time"] = when.strftime("%H:%M")
        else:
            claims.append({"chest": chest, "time": when.strftime("%H:%M")})
        last_click[chest] = when
    return claims


def to_int(text):
    """'74.739' -> 74739, '-120' -> -120 (the script writes thousands with dots)."""
    try:
        return int(text.replace(".", ""))
    except ValueError:
        return None


SESSION_LINE = re.compile(
    r"session: [\d.?]+ -> ([\d.]+), \+([\d.-]+) toques in (.+?) \(([\d.-]+)/s\).*"
    r"since launch: \+([\d.-]+) toques, (\d+) chests in (.+)$"
)


def counter_stats(entries):
    """Latest counter reading, last session's rate and totals since the script was launched."""
    stats = {"counter": None, "counter_at": None, "rate": None, "launch": None}
    for when, message in entries:
        if message.startswith("running:"):
            stats["launch"] = {"toques": 0, "chests": 0, "duration": "0m 00s", "since": when.strftime("%H:%M")}
            stats["rate"] = None
        reading = re.search(r"counter(?: at launch:)? ([\d.]+)$", message)
        if reading:
            stats["counter"], stats["counter_at"] = to_int(reading[1]), when.strftime("%H:%M:%S")
        session = SESSION_LINE.search(message)
        if session:
            stats["counter"], stats["counter_at"] = to_int(session[1]), when.strftime("%H:%M:%S")
            stats["rate"] = float(session[4])
            if stats["launch"] is not None:
                stats["launch"].update(toques=to_int(session[5]), chests=int(session[6]), duration=session[7])
    return stats


DROP_LINE = re.compile(r"new item: (.+) \((.*)\) (\$[\d.]+|no price)$")
SESSION_END_PREFIXES = ("you are back", "another window on top", "running", "stopped", "Bongo Cat is not open")


def parse_duration(text):
    """'1h 05m' or '3m 07s' (as the script writes them) -> seconds."""
    match = re.match(r"(\d+)h (\d+)m", text)
    if match:
        return int(match[1]) * 3600 + int(match[2]) * 60
    match = re.match(r"(\d+)m (\d+)s", text)
    return int(match[1]) * 60 + int(match[2]) if match else 0


def drops_today(entries):
    today = datetime.now().date()
    drops = []
    for when, message in entries:
        match = DROP_LINE.match(message)
        if match and when.date() == today:
            price = None if match[3] == "no price" else float(match[3][1:])
            drops.append({"time": when.strftime("%H:%M"), "name": match[1], "type": match[2], "usd": price})
    return drops[::-1]


def activity_today(entries):
    """Clicking periods today as [start, end] epoch milliseconds."""
    today = datetime.now().date()
    segments, start = [], None
    for when, message in entries:
        if when.date() != today:
            continue
        if message.startswith("idle: clicking") and start is None:
            start = when
        elif start is not None and message.startswith(SESSION_END_PREFIXES):
            segments.append([start.timestamp() * 1000, when.timestamp() * 1000])
            start = None
    if start is not None:
        segments.append([start.timestamp() * 1000, datetime.now().timestamp() * 1000])
    return segments


def sessions_today(entries):
    """Clicking sessions that logged a summary (script versions with OCR)."""
    today = datetime.now().date()
    sessions = []
    for when, message in entries:
        match = SESSION_LINE.search(message)
        if match and when.date() == today:
            seconds = parse_duration(match[3])
            sessions.append({
                "end": when.strftime("%H:%M:%S"),
                "start": datetime.fromtimestamp(when.timestamp() - seconds).strftime("%H:%M:%S"),
                "gain": to_int(match[2]),
                "seconds": seconds,
                "rate": float(match[4]),
                "counter": to_int(match[1]),
            })
    return sessions


def status():
    entries = read_log()
    code, text, since = script_state(entries)
    running = is_script_running()
    if running is False:
        code, text, since = "stopped", "The script is not running", None
    return {
        "now": datetime.now().strftime("%H:%M:%S"),
        "state": code,
        "state_text": text,
        "since": since.strftime("%H:%M:%S") if since else None,
        "timers": timers(),
        "chest_period": CHEST_PERIOD_SECONDS,
        "claims": claims_today(entries),
        "stats": counter_stats(entries),
        "drops": drops_today(entries),
        "activity": activity_today(entries),
        "sessions": sessions_today(entries),
        "now_ms": datetime.now().timestamp() * 1000,
        "log": [f"{w.strftime('%H:%M:%S')} {m}" for w, m in entries[-25:]][::-1],
    }


def inventory_snapshot():
    with state_lock:
        items = []
        total = 0.0
        for item in inventory["items"]:
            price = prices["items"].get(item["hash_name"], {})
            row = dict(item, price_usd=price.get("usd"), listings=price.get("listings"))
            if row["price_usd"] is not None and item["marketable"]:
                total += row["price_usd"] * item["count"]
            items.append(row)
        return {
            "items": items,
            "total_usd": round(total, 2),
            "updated": inventory["updated"],
            "error": inventory["error"],
            "account": inventory["account"],
            "prices_updated": prices["updated"],
            "prices_error": prices["error"],
        }


def fetch_inventory():
    """(items grouped by name, {assetid: (name, type, hash_name)}) of the current Steam account."""
    steam_id, account, persona = steam_account()
    with state_lock:
        inventory["account"] = persona or account
    url = INVENTORY_URL.format(steam_id=steam_id, app=APP_ID)
    all_assets, descriptions = [], {}
    page_url = url
    while True:  # Steam returns at most INVENTORY_PAGE items per request
        data = http_json(page_url)
        all_assets += data.get("assets", [])
        descriptions.update({(d["classid"], d["instanceid"]): d for d in data.get("descriptions", [])})
        if not data.get("more_items"):
            break
        page_url = f"{url}&start_assetid={data['last_assetid']}"
        time.sleep(INVENTORY_PAGE_GAP_SECONDS)
    grouped = {}
    assets = {}
    for asset in all_assets:
        d = descriptions.get((asset["classid"], asset["instanceid"]))
        if not d:
            continue
        key = d["market_hash_name"]
        assets[asset["assetid"]] = (d.get("name", key), d.get("type", ""), key)
        if key not in grouped:
            grouped[key] = {
                "name": d.get("name", key),
                "hash_name": key,
                "type": d.get("type", ""),
                "icon": ICON_URL.format(d["icon_url"]) if d.get("icon_url") else None,
                "marketable": bool(d.get("marketable")),
                "tradable": bool(d.get("tradable")),
                "count": 0,
            }
        grouped[key]["count"] += int(asset.get("amount", 1))
    return sorted(grouped.values(), key=lambda i: i["name"].lower()), assets


def log_new_items(assets):
    """Append every asset not seen before to the script's log; the first run only records."""
    try:
        with open(KNOWN_ASSETS_PATH, encoding="utf-8") as f:
            known = set(json.load(f))
    except (OSError, ValueError):
        known = None
    if known is not None:
        new = [assets[a] for a in assets if a not in known]
        with state_lock:
            market = dict(prices["items"])
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            for name, kind, hash_name in new:
                price = market.get(hash_name, {}).get("usd")
                value = f"${price:.2f}" if price is not None else "no price"
                f.write(f"{stamp} new item: {name} ({kind}) {value}\n")
    with open(KNOWN_ASSETS_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(set(assets) | (known or set())), f)


def inventory_worker():
    try:
        with open(INVENTORY_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        with state_lock:
            inventory.update(items=saved["items"], updated=saved["updated"], account=saved.get("account"))
    except (OSError, ValueError, KeyError):
        pass
    while True:
        try:
            items, assets = fetch_inventory()
            log_new_items(assets)
            updated = datetime.now().strftime("%d/%m %H:%M")
            with state_lock:
                inventory.update(items=items, updated=updated, error=None)
                account = inventory["account"]
            with open(INVENTORY_PATH, "w", encoding="utf-8") as f:
                json.dump({"items": items, "updated": updated, "account": account}, f)
            wait = INVENTORY_REFRESH_SECONDS
        except Exception as error:  # network or Steam errors only change what the panel shows
            message = str(error)
            if "429" in message:
                message = "Steam is rate-limiting inventory requests (429); retrying in 10 min"
            elif "403" in message:
                message = "The Steam inventory is private (403)"
            with state_lock:
                inventory["error"] = message
            wait = INVENTORY_RETRY_SECONDS
        time.sleep(wait)


def fetch_market():
    """Lowest listing price (USD) of every item of the game on the Steam market."""
    found = {}
    start = 0
    while True:
        data = http_json(MARKET_URL.format(app=APP_ID, start=start))
        results = data.get("results") or []
        page = {
            r["hash_name"]: {"usd": r.get("sell_price", 0) / 100, "listings": r.get("sell_listings", 0)}
            for r in results
        }
        found.update(page)
        with state_lock:  # show each page as it arrives, so a 429 midway keeps what we got
            prices["items"].update(page)
            prices["updated"] = prices["updated"] or "loading…"
        start += len(results)
        if not results or start >= data.get("total_count", 0):
            return found
        time.sleep(MARKET_PAGE_GAP_SECONDS)


def market_worker():
    try:
        with open(PRICES_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        with state_lock:
            prices.update(items=saved["items"], updated=saved["updated"])
    except (OSError, ValueError, KeyError):
        pass
    while True:
        try:
            found = fetch_market()
            updated = datetime.now().strftime("%d/%m %H:%M")
            with state_lock:
                prices.update(items=found, updated=updated, error=None)
            with open(PRICES_PATH, "w", encoding="utf-8") as f:
                json.dump({"items": found, "updated": updated}, f)
            wait = MARKET_REFRESH_SECONDS
        except Exception as error:  # network or Steam errors only change what the panel shows
            message = str(error)
            if "429" in message:
                message = "Steam is rate-limiting price requests (429); retrying in 10 min"
            with state_lock:
                prices["error"] = message
            wait = MARKET_RETRY_SECONDS
        time.sleep(wait)


def cat_png():
    try:
        window = bongo_auto.game_window()
        if not window:
            return None
        bongo_auto.update_layout(window)
        image = bongo_auto.grab(bongo_auto.VIEW_BOX)
    except OSError:  # screen not readable, e.g. the PC is locked
        return None
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


PAGE_PATH = os.path.join(HERE, "panel.html")  # read on every request: edits show on reload


class Handler(BaseHTTPRequestHandler):
    def send(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            with open(PAGE_PATH, "rb") as f:
                self.send(f.read(), "text/html; charset=utf-8")
        elif path == "/api/status":
            self.send(json.dumps(status()).encode(), "application/json")
        elif path == "/api/inventory":
            self.send(json.dumps(inventory_snapshot()).encode(), "application/json")
        elif path == "/cat.png":
            png = cat_png()
            if png:
                self.send(png, "image/png")
            else:
                self.send(b"", "image/png", status=503)
        else:
            self.send(b"not found", "text/plain", status=404)

    def log_message(self, *args):
        pass


def main():
    threading.Thread(target=inventory_worker, daemon=True).start()
    threading.Thread(target=market_worker, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Panel at http://localhost:{PORT}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
