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
| Overview | EXP/h, gold/h (incl. sold items), runs/h, DPS per run, time to the next level |
| Character Stats | Your character sheet, kept separately for each of your characters |
| Stages | Your stages compared: time, EXP/h, gold/h, enemy damage types |
| Drops | Drops by rarity, gems by tier, runes, keys |
| Deaths | Who killed you and with what |
| Item Comparer | Item comparison with F8: values like in the game, differences, effects, verdict |
| BiS Gear | Best theoretical item per slot for your class and mode, compared with what you wear, and where it drops |
| Talents | Talent planner (afkmeta style): build your tree, compare it with your current build, let it find the best build for a mode |

**Skill tracking** (Controls): counts which abilities you cast per run from the skill bar and estimates their damage share – shown on the Stages page and usable on the Talents page.
| Gems | Which gem helps you most |
| Weights | Your own values for effects that cannot be calculated |

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
- Some legendary effects cannot be calculated – enter your own value on the **Weights** page.
- Empty sockets are rated with the best gem of the tier chosen on the **Gems** page.
- Text recognition can misread. Implausible values are marked yellow with "?".

## Reporting problems
Please open an [issue](../../issues) and attach:
- for misread items: the matching files from the `captures/` folder (image + `.json`),
- for crashes: `tracker_errors.log`.

Both are in the tracker's folder.

---
Unofficial fan tool, not affiliated with First Day Games.
