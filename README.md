# Bongo Auto

Farms [Bongo Cat](https://store.steampowered.com/app/3419430/Bongo_Cat/) (Steam) while you are away
from the PC: it clicks the cat to raise the toque counter and claims both chests (items and emotes)
every 30 minutes. A local web panel shows what it is doing, the drops and your inventory with market
prices.

## Requirements

- Windows 10/11 and Python 3 with Pillow (`pip install pillow`).
- Bongo Cat running on the **primary monitor at 2560×1440** (the cat, chest and counter positions are
  hard-coded for that resolution; see [Adjusting positions](#adjusting-positions)).
- In Bongo Cat's settings, *always show chests* on (the script clicks them next to the cat).
- The PC must stay **unlocked**: Windows ignores simulated input on the lock screen. While the script
  clicks, Windows sees activity, so it does not lock by idle on its own.

## How to start it

| What | Run | Does |
|---|---|---|
| Start / restart the farmer | `Lanzar.bat` | Stops any running copy, then starts `bongo_auto.py` minimized. The cat is moved into position. |
| Stop the farmer | `Parar.bat` | Stops `bongo_auto.py`. |
| Open the panel | `Panel.bat` | Restarts `panel.py` and opens http://localhost:8765. |

Start Bongo Cat first, then `Lanzar.bat`. Tip: create desktop shortcuts to the three `.bat` files
(right click → *Send to* → *Desktop*); set them to run minimized.

From a terminal: `python bongo_auto.py` (Ctrl+C to stop), `python bongo_auto.py --place` (only move
the cat and exit), `python panel.py`.

## What the farmer does

1. **Places the cat**: F1 resets it to the centre of the screen, then it is dragged to the bottom-right
   corner. This is repeated before every clicking session, because moving the mouse while a click is
   held drags the cat away.
2. **Clicks only when you are away**: after 15 s without keyboard/mouse input it clicks the cat at
   ~12–14 clicks/s (the fastest rate the game counts). Touching the mouse or a key pauses it at once.
3. **Checks the clicks count**: every 20 s it compares the counter on screen. If it has not moved, it
   moves the mouse away and back; if it is still stuck, it places the cat again.
4. **Claims chests**: reads the game's chest timers from the registry
   (`HKCU\Software\Irox Games\BongoCat`) and clicks a chest when it is ready. A chest costs 1,000 toques.
5. **Logs sessions**: the counter is read with Windows OCR (`ocr.ps1`) at the start and end of each
   session, so each session logs toques gained, duration, rate and totals since launch.

The farmer does not sign in anywhere: it clicks the Bongo Cat window, so it farms for whichever
Steam account has the game open.

## The panel

`panel.py` serves http://localhost:8765 (only reachable from this PC):

- script state, live view of the cat, counter, toques since launch and rate of the last session;
- chest countdowns, clicking activity over the last 3 hours, toques per session;
- drops of the day and the log, with filters;
- the Steam inventory of the game, with search, filters, sorting, pages and the lowest market price.

The Steam account is the last one signed in to Steam on this PC (from `config/loginusers.vdf`). The
inventory must be public. Steam rate-limits its public endpoints: the inventory is fetched every
10 min and the whole market of the game every 30 min (retrying after 10 min on HTTP 429).

## Files it writes

| File | Content |
|---|---|
| `bongo_auto.log` | Activity log. Rotates at 5 MB to `bongo_auto.log.1` … `.3`. |
| `prices.json` | Last market prices, so they survive panel restarts. |
| `known_assets.json` | Inventory items already seen, to log only new drops (`new item: …`). |
| `%TEMP%\bongo_counter.png` | Counter crop passed to OCR, overwritten on every read. |

No screenshots are kept: the live view and counter checks are made in memory.

## Adjusting positions

All coordinates are constants at the top of `bongo_auto.py` (`CAT_HEAD`, `CHESTS`, `COUNTER_BOX`,
`CAT_DEFAULT_GRAB`, `CAT_DRAG_TO`) and `panel.py` (`CAT_BOX`), in physical pixels of the primary
monitor. On another resolution, run `python bongo_auto.py --place`, then measure where the cat's head,
both chests and the counter end up and update those values.
