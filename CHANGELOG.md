# Changelog

All changes of the Deskrawl Tracker, newest first. Also shown in the app under **Controls → What's new**.

## v1.0.24 – 2026-10-10

### Game patch 1.0.2
- **Talents updated to patch 1.0.2** (from the afkmeta.com planner), e.g.:
  - Sorcerer: Combustion 300 %, Storm Conduit 30 %, Arcane Exposure every 4th Strong Attack plus +150 % damage vs Vulnerable, Shattering Ice +250 % Critical Hit Damage vs Frozen.
  - Hunter: Lone Hunter, Exposing Traps, Critical Injection, Arcane Corrosion; Lethal Traps replaces Oversized Traps.
  - Monk: Chain Force, Exposing Strikes, Iron Constitution.
  - The best build counts all of the new effects.
- **Abilities, statuses and items from the patch notes:**
  - Ice Shards 115 %, Flame Lightning 60 % + 60 %, Venom Bolt 160 % with 2 Poisoned stacks, Poison Trap 3 stacks.
  - Frost Trap is now Arcane Trap (Arcane Grip holds enemies for 5 s).
  - Burn lasts 4 s, Electrostatic stacks up to 10, Poisoned deals 75 % per stack (up to 150 stacks).
  - Staff of the Frostwyrm, Violet Skybow and Soulrender changes.
  - Critical Damage Reduction is capped at 100 %.

### Improved
- **More accurate best build.** Basic Attacks hardly flash on the skill bar, so they were counted far too rarely (e.g. Flame Lightning 7 instead of ~100 a minute). The talent calculator now takes their rate from the combat simulation. Electrostatic's value is fitted to a training dummy test.
  - Kill-based talents are capped so a calibration that failed (e.g. stats of another character) cannot inflate them.
  - A note appears when your own damage shares are set under "Edit rating values".
- **Your own Legendary sound.** Put .mp3 or .wav files into the "sounds" folder next to the tracker (**Sounds folder** button on the Drops page) and choose one there. MP3 now plays too. The built-in chime stays the default.

Your settings, character data and histories are kept when you update.

## v1.0.23 – 2026-10-07

### Improved
- **The best build goes straight into the talent tree** below the recommendation once it is calculated. **My build in tree** puts your own build back.

### Fixed
- **"Load current build" reads the talent window correctly.** Before, it matched talent pictures anywhere on the screen and even "found" a build with no talent window open. Now:
  - It only reads when the talent window is open ("Combat Talents").
  - It finds the rank badges, works out which rows of the tree are visible, and reads every number in the game's font ("Ø" = 0).
  - It takes your total talent points from the window ("0/70").
  - Numbers it is not sure of are checked against the points spent. This also covers the talent hidden under the Reset button, and keeps one capstone per row.
  - Open the talent window, click **Load current build**, scroll to the bottom and click it again.

Your settings, character data and histories are kept when you update.

## v1.0.22 – 2026-10-07

### New
- **The Talents page is back, with "Best build for your class".** Click **Calculate best build** and the tracker works out the best talent build for your class in the chosen mode (Damage, Survival, Balanced or Farming) and compares it with your current build. It is calculated from:
  - your character sheet (F9), using the same damage formula and survival model as the Item Comparer,
  - skill tracking: which abilities you use, how often, and their share of your damage,
  - how often enemies carry Burn, Electrostatic, Vulnerable, Frozen and other statuses, including the ones your talents apply (e.g. Ignite on critical hits),
  - a combat simulation for cooldown, mana cost, mana gain and free-cast talents.
- **The build has to fit together.** For each status, the page shows what applies it, how often enemies carry it and which talents use it (e.g. "Burn: Ignite → on enemies 49 % of the time → used by Conflagration, Combustion").
  - It warns when a talent needs a status that nothing in the build applies, or when only part of its bonus works.
  - Each talent gets a role: Synergy, Damage, Survival, or Filler (only opens the next row).
  - Effects that do nothing for you are marked, e.g. Fire Damage without Fire skills.
- **Show in planner** loads the suggested build into the talent planner, and **Copy link** copies it as an afkmeta.com link.
- The planner's numbers ("Compared with my build", value of the next point) use the same calculation.

Load your current build once ("Load current build" with the talent window open), so that the talents you already have are not counted twice. Your settings, character data and histories are kept when you update.

## v1.0.21 – 2026-10-07

### Improved
- **Space now takes a Legendary all the way into the inventory.**
  - For a Legendary on the ground, the tracker presses Space once to move it into the carriage, and 1.5 seconds later once more to move it from the carriage into the inventory.
  - If no "Obtained …" pop-up with its name follows within 4 seconds, Space is pressed one more time.
  - When the Legendary was only seen on the stage end screen, it is already in the carriage, so Space is pressed once.

Your settings, character data and histories are kept when you update.

## v1.0.20 – 2026-10-07

### New
- **Space is pressed in the game when a Legendary drops**, so the item goes into your inventory. It is pressed once, at the same moment as the sound.
  - If Deskrawl has focus, the key is pressed right away.
  - Otherwise the tracker briefly brings the game to the front and gives focus back, the same way it opens the log panel.
  - Turn it off on the **Drops** page with **Press Space in the game when a Legendary drops**.

Your settings, character data and histories are kept when you update.

## v1.0.19 – 2026-10-07

### Fixed
- **The Legendary sound played over and over in town.** Orange headings of the game's panels (e.g. "CHARACTER", "Health", "Toughness") were taken for item names on the ground. The tracker now looks for drops only during a run and a few seconds after it, and only counts names of real Legendary and Divine items.

Your settings, character data and histories are kept when you update.

## v1.0.18 – 2026-10-07

### Fixed
- **Legendaries on the stage end screen were sometimes missed.** The "Legendary 3 (+1)" row is now read reliably, so every Legendary counts in Legendaries/h and plays the sound.
- **No more second sound when the carriage unloads.** When the carriage empties, the item's name shows above it like a new drop. The tracker now waits 4 seconds for the "Obtained …" pop-up and ignores those names, so the sound for a drop on the ground comes about 4 seconds later.

Your settings, character data and histories are kept when you update.

## v1.0.17 – 2026-10-07

### New
- **Sound when a Legendary drops.** It plays the moment an orange item name appears on the ground. If one is missed there (it dropped off screen or straight into the carriage), the stage end screen plays it. You can turn it off and try it with **Play sound** on the **Drops** page, which also shows how many Legendaries dropped this session and the last one.

### Improved
- **Legendaries/h now counts every drop.** The count comes from the stage end screen ("Legendary 2 (+1)"), so it also includes items that were sold right away or never picked up. Before, only picked-up items counted. Divine items count too.

Your settings, character data and histories are kept when you update.

## v1.0.16 – 2026-10-07

### New
- **Counted casts** (Skill Tracking page) shows every ability's casts from the runs watched with skill tracking: how many runs, total, average per run, min, max and per minute.
- **Edit rating values…** (Skill Tracking page) lets you set the values the Item Comparer, Minions and BiS use by hand. The measured value is shown next to each field, and an empty field keeps the measurement. **Reset to measured** removes your values. You can set:
  - the damage share of each ability,
  - damage over time,
  - how often enemies are Burning, Slowed, Vulnerable, Poisoned, Bleeding, Stunned or Immobilized,
  - kills per second for the combat simulation.

### Fixed
- **The first wave of a stage was not counted.** The skill bar is now checked in the background at the start of a run, and counting goes on meanwhile.
- **Damage shares now use the counted casts.** Before, Basic Attacks were estimated from attack speed, which gave e.g. Flame Lightning 46% with only ~2 casts per run. The estimate is now an option in **Edit rating values…**.

Your settings, character data and histories are kept when you update.

## v1.0.15 – 2026-10-07

### Improved
- **Item Comparer follows the game's damage formula** (from the wiki):
  - Primary attribute, element plus All Damage, critical hits, Damage vs …, and the attack type bonus.
  - Damage over time ticks get no attack type bonus.
  - Survival uses the game's Toughness and Recovery (regeneration + attacks/s × Life on Hit + 0.25 × Life on Kill).
- **Intelligence, Strength and Dexterity also defend every hero**: +1 Magic Resist per Intelligence, +1 Armor per Strength, and Critical Damage Reduction from Dexterity. 1 point of your primary attribute is still +1% damage.
- **Now rated** (these counted 0 before):
  - Bonus All Damage
  - Special Ability Bonus Damage
  - Damage vs Slowed, Immobilized, Bleeding, Burned, Poisoned and Vulnerable
  - Damage Over Time
  - Critical Damage Reduction
  - Life on Kill
- **Mana and cooldowns: a combat simulation** of the game's auto-combat rules counts how many casts more mana or less cooldown give. It covers cast order, the global cooldown, mana cost and Mana on Kill / Regeneration. It is fitted to the casts skill tracking counted. Mana on Kill, Mana Regeneration, Max Mana, Mana Cost Reduction, Cooldown Reduction and Attack Speed are now rated by the extra casts. This also applies to minions with mana bonuses.
- **With skill tracking**, your measured shares of Basic / Strong / Special damage, damage over time and status uptimes replace the fixed estimates.
- **More legendary effects are rated**:
  - Damage over time bonus, potion charges, healing while moving, and move speed after kills.
  - Bonuses "while <ability> is active", using the ability's uptime.
  - Damage pulses, a proc every N hits, raining daggers, extra targets and pierce, and "every Nth cast".
  - Effects for abilities you do not use now say so.

Your settings, character data and histories are kept when you update.

## v1.0.14 – 2026-10-07

### New
- **Read skill bar** (Skill Tracking page): reads the abilities on your skill bar now and shows them as the game draws them, each with the ability the tracker took it for.
  - If a name is wrong, correct it and press **Use these skills**. The tracker keeps the game's icon and recognises that ability by it from then on, including icons the wiki does not have.

### Fixed
- **Skill tracking kept old skills** after you swapped abilities. The skill bar is now read again at the start of every run.
- **Wrong abilities on the skill bar.** The bar is read from several pictures and each slot is voted on, so a cast flash, a cooldown sweep or an effect no longer misleads it. The slot spacing is found reliably, and the potion and scroll slots are left out.
- Finding the skill bar is about 3× faster.

Your settings, character data and histories are kept when you update.

## v1.0.13 – 2026-10-06

### New
- **Skill Tracking page**:
  - Turn skill tracking on or off.
  - See the detected skill bar with pictures and the casts of the running run.
  - See your build per stage: casts per run and per minute, % weapon damage per second, and the damage share of each ability, slot and element.
  - See how much of the time enemies are Burning, Chilled, Vulnerable and so on, and every counted run.
  - **Use for ratings** takes the measured shares for the item rating.
- **Minions** show their pictures.

### Improved
- **Minion values are measured from your build** once skill tracking has counted enough runs (it uses the newest 40):
  - Element bonuses count with your share of damage in that element.
  - Basic Attack, Strong Attack and special ability bonuses count with your measured damage shares.
  - "Damage to Burning / Slowed / Vulnerable … enemies" counts with how often your abilities, and the minion itself, put enemies in that state. This uses the status durations from the wiki.
  - Damage abilities with a number (e.g. 200% Frost damage every 6 s) are compared with your damage per second.
  - Minions that apply Vulnerable count its +30% damage.
  - The page says whether values are measured or estimated.
- **Basic Attacks** hardly flash on the skill bar. Their count is now estimated from your attack speed.

Your settings, character data and histories are kept when you update.

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
