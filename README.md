# Deskrawl Tracker

Companion tool for **Deskrawl**: shows EXP/h, gold/h, runs, deaths, drops and stage comparisons live, and
rates items at the press of a key – "equip or not?" in terms of damage, survival and income.

The tracker reads only two things:
- the game's **log file** (`Game.log`) and
- the **game picture** (text recognition, like a screenshot).

It does **not** read game memory, does not change any game files and sends nothing to the internet.

## Features

| Page | Content |
|---|---|
| Overview | EXP/h, gold/h (incl. sold items), runs/h, DPS per run, time to the next level – at level 70 to the next Paragon level (Paragon EXP added up from the level-70 runs in the game log) |
| Character Stats | Your character sheet, kept separately for each of your characters |
| Stages | Every region and stage in map order (incl. Dream realms), stages without runs marked; **Runs…** lists single runs to delete them, **Clear this stage** starts a stage over; time, EXP/h, gold/h, enemy damage types, with search, type and difficulty filters and sorting (incl. map order). **Boss farming**: Silver and Gold bosses ranked by kills/h or efficiency (kill speed + EXP, adjustable weighting), with legendaries/h |
| Drops | Drops by rarity, gems by tier, runes, keys |
| Deaths | Who killed you and with what |
| Item Comparer | Item comparison with F8: values like in the game, differences, effects, verdict |

**Skill tracking** (Controls): counts which abilities you cast per run from the skill bar and estimates their damage share – shown on the Stages page.
| Item Database | Every Legendary and Divine item by slot: unique effect, base values and attribute ranges, attribute pool per class, Ancient / Black Mist variants and where it drops |
| Minions | All 67 minions ranked by what their passives and buffs are worth for your character (damage, survival, farming), with where their Rein drops |
| Gems | Which gem helps you most |

## Installation

### Option A: ready-made .exe (recommended)
1. Download the latest `DeskrawlTracker.zip` from [Releases](../../releases).
2. Unzip it, e.g. to `Documents\DeskrawlTracker`.
3. Start `DeskrawlTracker.exe`.

Windows SmartScreen may warn on the first start ("Unknown publisher") because the file is not signed:
**More info → Run anyway**.

### Option B: from source
1. Install [Python 3.12](https://www.python.org/downloads/) (tick "Add python.exe to PATH").
2. Download the repo (green **Code → Download ZIP** button) and unzip it.
3. Double-click `install.bat`.
4. Start with `Start Deskrawl Tracker.bat`.

### First start
A setup window checks:
1. **Log file** – usually found automatically
   (`%USERPROFILE%\AppData\LocalLow\First Day Games\Deskrawl\Game.log`), otherwise pick it with "Browse…".
2. **Windows text recognition (English)** – missing on some non-English Windows installations.
   The window then shows the command to install it (PowerShell as administrator):
   ```powershell
   Add-WindowsCapability -Online -Name "Language.OCR~~~en-US~0.0.1.0"
   ```

You can open this window again later via **Controls → Change log file**.

### Updates
The tracker checks GitHub for a new release at every start (and via **Controls → Check for updates**). **Update now** downloads it, replaces the program files and restarts – your settings, character data and histories stay.
**Controls → What's new** lists every version and its changes (also in [CHANGELOG.md](CHANGELOG.md)); after an update it opens once by itself. The Controls at the bottom of the sidebar are folded by default – click **▸ Controls** to open them.
Before anything is replaced the download is checked against the size and SHA-256 checksum GitHub lists for the release; a damaged or incomplete download is discarded and nothing changes.

## Usage

| Key | Action |
|---|---|
| **F8** | Hover an item (tooltip open) → the item is read and rated |
| **F9** | Character window open on the Attributes tab → your values are read |
| **F10** | Hide Deskrawl (it keeps running) or show it again |

Important: do **not minimize** Deskrawl – a minimized window cannot be read.
Use F10 instead: the game keeps running invisibly and clicks go through it.

First steps after the setup, once for each of your characters:
1. Open the character window, press **F9** at the top of the attribute list, scroll down, press **F9** again.
2. Hover your equipped weapon and press **F8** (the tracker remembers its damage).

When you log in with another character, the tracker switches to that character's values automatically.

## Limits
- Attack speed counts fully towards damage; abilities with a cooldown gain less in reality.
- Some legendary effects cannot be calculated – they are shown but not rated.
- Empty sockets are rated with the best gem of the tier chosen on the **Gems** page.
- Text recognition can misread. Implausible values are marked yellow with "?".

## Reporting problems
Please open an [issue](../../issues) and attach:
- for misread items: the matching files from the `captures/` folder (image + `.json`),
- for crashes and other errors: `tracker_errors.log` (it also notes errors the tracker recovered from).

Both are in the tracker's folder.

Settings and stage statistics are saved crash-safe. If a file is ever damaged, the tracker starts with the backup it keeps next to it (`tracker_config.json.bak`, `stage_stats.json.bak`) and keeps the damaged file as `.broken-<date>`.

---
Unofficial fan tool, not affiliated with First Day Games.
