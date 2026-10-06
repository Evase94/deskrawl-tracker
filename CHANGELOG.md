# Changelog

All changes of the Deskrawl Tracker, newest first. Also shown in the app under **Controls → What's new**.

## v1.0.12 – 2026-10-06

### New
- **Minions** (new page): all 67 minions ranked by what their passives and buff abilities are worth for your character: damage, survival and farming, with a score for the chosen mode.
  - It uses your character sheet (F9) and the same model as the Item Comparer.
  - Conditional bonuses ("+30% Damage to Slowed enemies") count for an estimated share of the time. Timed buffs count for their uptime, and heals and shields count as regeneration.
  - Abilities that deal damage, and mana bonuses, are shown but not rated.
  - Each minion shows where its Rein drops and the drop chance.

### Fixed
- **Paragon:** when the game showed the next Paragon level a moment before its run reached the log, the tracker switched to an estimate ("≈"). It now stays exact. A total already marked as an estimate is counted again from the log if the run that reached level 70 is still in it.

Your settings, character data and histories are kept when you update.

## v1.0.11 – 2026-10-06

### Fixed
- **Item Comparer swapped the items** when your equipped item was upgraded (e.g. "+1"). The game shows the upgrade bonus of the worn item in square brackets ("579 Armor [+28]"), and the tracker took those for comparison numbers. Now:
  - the tooltip under "Equipped" is always the worn item,
  - values in square brackets count as upgrade bonus, and only round brackets "(+173)" count as comparison.
- Entries in "Recently checked" from before this fix may be the wrong way round. Remove them with **Clear list**.

Your settings, character data and histories are kept when you update.

## v1.0.10 – 2026-10-06

### New
- **Look at and delete single runs.** **Stages → Runs…** lists every run of the character, newest first, with its stage, difficulty, run time, EXP, gold, items, legendaries, death and DPS.
  - Filter by stage.
  - Select one or more runs and click **Delete selected runs** (or press Del). They are taken out of the stage statistics.
- **Start a stage over.** Click a stage on the Stages page, then **Clear this stage…**. All data of that stage on that difficulty is deleted, and new runs are counted from scratch. **Clear stage…** is also in the Runs window.
- Runs are now stored one by one. Runs recorded before this version stay in the totals and can only be removed with **Clear stage**.

### Changed
- Item data updated from the wiki: Soulrender's new effect (every hit grants a stack), and the "All ability levels +10" of Remnant of the Elder Sage now counts in the item rating.

Your settings, character data and histories are kept when you update.

## v1.0.9 – 2026-10-06

### New
- **Item Database** (new page after Item Comparer): every Legendary and Divine item of the game by slot.
  - Filter by slot, rarity, class or "Black Mist possible", and search by name, effect, attribute or boss.
  - Each item shows its unique effect and where it drops (boss and chance, enemy level, chests, Mystery vendor).
  - Legendary items show base values by item level, attribute ranges at item level 700/850 and the attribute pool per class.
  - Divine items show their fixed numbers.
  - The Ancient and Black Mist variants are explained for each item.
- **What's new** (Controls): all versions and their changes, newest first. It opens once by itself after an update.

### Changed
- The Controls in the sidebar are folded by default. Click **▸ Controls** to open them.
- BiS Gear, Talents and Weights are hidden from the menu for now. Values you set there still count in the item rating.

Your settings, character data and histories are kept when you update.

## v1.0.8 – 2026-10-06

### New
- **Paragon progress for level-70 characters.** The Overview now shows the time and runs to the next Paragon level. Before, it showed a wrong "to level 71" bar.
  - Paragon XP is added up from all level-70 runs in the game log (Game.log and the previous session's Game-prev.log). Each run counts once, and the total is kept by the tracker.
  - Level and progress follow from the Paragon XP table. Paragon is shared by all characters of the account.
  - The Paragon level shown in the game ("Lv. 70 (N)", bottom left) is read as a check. If the log no longer contains the run that reached level 70, the tracker starts from that level and marks the values with "≈" as estimates.
- The header shows your Paragon level next to the character level.

Your settings, character data and histories are kept when you update.

## v1.0.7 – 2026-10-06

### New
- **Stages → All stages** shows every region and stage in map order:
  - Each region is a group with its level range and total runs, stages 1–7 below it.
  - The three Dream realms (Treasure Key maps) are included: Dreamy Kings Woods, Dreamy Barrens and Dreamy Keep.
  - Stages you have not played yet (on the chosen difficulty) are grey and marked "no data". A checkbox hides them.
  - Clicking a column header sorts inside each region.
  - Clicking a stage without runs shows its enemies, boss and drops.

### Fixed
- Stage details for "Kings Woods: 4" (and similar names) showed the enemies of "Kings Woods South: 4".

Your settings, character data and histories are kept when you update.

## v1.0.6 – 2026-10-06

### New
- **Stages → All stages** now has filters and sorting:
  - Search by region, stage or boss.
  - Filter by type (normal stages, Silver bosses, Gold bosses, unknown) and by difficulty.
  - Sort by EXP/h, gold/h, items/h, run time, runs, deaths, DPS or map order.
  - A new **Boss** column shows Silver or Gold.

### Fixed
- Inferno runs (logged by the game as "Inferno1") were missing from Boss farming and the difficulty filter. They now count as Inferno. Drop item level, Soul Shards and BiS stage hints also use the right difficulty.

Your settings, character data and histories are kept when you update.

## v1.0.5 – 2026-10-06

### New
- **Stages → Boss farming**: ranks the Silver bosses (stage 4 of a region, they drop the skulls) and Gold bosses (last stage, more legendaries) you have farmed.
  - Rank by **Time (kills/h)** or **Efficiency (time + EXP)**: a 0–100 score from kills/h and EXP/h, with a slider for the weighting.
  - Search by region, boss or stage; filter by boss type and difficulty.
  - **Legendaries/h** per stage (counted from this version on).

### Improved
- Settings and stage statistics are saved crash-safe. A backup (`.bak`) is kept, and a damaged file is restored from it automatically.
- Updates are verified before anything is replaced: origin, size, SHA-256 checksum, a complete zip and write access to the folder. A failed update is reported on the next start.
- Errors that were ignored before are now written to `tracker_errors.log`. The file is limited to 1 MB and keeps 2 older copies. Please attach it when you report a problem.

Your settings, character data and histories are kept when you update.

## v1.0.4 – 2026-10-05

### Fixed
- **Update installation**: the update no longer opens an empty command window that waits until it is closed by hand. Downloading, replacing the files and restarting now run without any window.

**Updating from 1.0.1 – 1.0.3:** those versions still contain the old updater, so this one update may show the empty window once more – just close it and the update finishes. From 1.0.4 on it runs silently.

### Install / update
Message at start, or **Controls → Check for updates**. From 1.0.0: unzip `DeskrawlTracker.zip` and copy the folder over the old one – your settings stay.

## v1.0.3 – 2026-10-05

### New
- **Smoother window**: moving and resizing the tracker no longer stutters. Only the visible page is laid out, text wrapping and table columns update once resizing pauses, and the background readers pause while the window is dragged.
- **Stage end screen**: run time, EXP and gold of each run are read from it; the *Time* columns of Recent runs and Stages show the real run time. A run whose CLEARS count did not go up is recorded as a death.

### Install / update
1.0.1 and later update themselves (message at start, or **Controls → Check for updates**). From 1.0.0: unzip `DeskrawlTracker.zip` and copy the folder over the old one – your settings stay.

## v1.0.2 – 2026-10-05

### New
- **Stage names from the stage end screen**: after every run the tracker reads the end screen (stage, run duration, clears). The log panel (C) is only opened when that screen was not seen.
- **Deaths**: detected from the death screen (YOU DIED / AUTO REVIVE AFTER … / TOWN). Item tooltips with "Revive Cooldown" no longer count as deaths, and a death no longer opens the log panel.
- **Talents**
  - *Load current build* reads your build from the game's talent window (scroll and click again for the lower rows).
  - New button set: Load current build, Save build, Reset, Share build, Load build…; *Best build* removed.
  - *All saved builds* window: compare every saved build with your build, load or delete it.

### Install / update
1.0.1 and later update themselves (message at start, or **Controls → Check for updates**). From 1.0.0: unzip `DeskrawlTracker.zip` and copy the folder over the old one – your settings stay.

## v1.0.1 – 2026-10-05

### New
- **Automatic updates**: the tracker checks GitHub at every start and via **Controls → Check for updates**. *Update now* downloads the new version, replaces the program files and restarts – settings, character data and histories stay. (Coming from 1.0.0: download this zip once by hand, from now on it updates itself.)
- **Talents**: share your build as an afkmeta.com link (*Share build*), load a link or code (*Load build…*), and save named builds to compare their damage and survival against your own build.
- **Legendary effects** that depend on your abilities (procs, extra projectiles, crit damage for one ability, +ability levels) are now rated in the Item Comparer, BiS Gear and the Weights page – using the ability shares from the Talents page or skill tracking.
- **Overview**: peak DPS per run in the Recent runs table.
- **Item Comparer**: *Clear list* button for "Recently checked".

### Install / update
Unzip `DeskrawlTracker.zip` and start `DeskrawlTracker.exe` (or copy the folder over your 1.0.0 folder – your settings stay). Windows SmartScreen may warn because the file is not signed: **More info → Run anyway**.

## v1.0.0 – 2026-10-05

First release of the Deskrawl Tracker.

### Download
Unzip `DeskrawlTracker.zip` and start `DeskrawlTracker.exe`. Windows SmartScreen may warn because the file is not signed: **More info → Run anyway**.

On the first start a setup window finds your `Game.log` and checks that Windows' English text recognition is installed.

### Features
- **Overview**: EXP/h, gold/h (incl. sold items), runs/h, DPS per run, time to the next level
- **Character Stats**: read with F9, kept separately for each of your characters
- **Stages**: your stages compared, enemy damage types, forecast for the next difficulty, ability casts per run
- **Drops / Deaths**: drops by rarity, gems by tier; who killed you and with what
- **Item Comparer** (F8): both items like in the game, differences, effects, verdict, gems and weapon damage included
- **BiS Gear**: best theoretical item per slot for your class and mode, where it drops
- **Talents**: talent planner with "best build" for a mode
- **Skill tracking**: counts ability casts from the skill bar and estimates their damage share
- **F10** hides Deskrawl without minimizing it, so the tracker keeps reading

The tracker reads only the game's log file and the game picture. It does not read game memory and does not change game files.

Unofficial fan tool, not affiliated with First Day Games.
