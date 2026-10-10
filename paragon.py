"""Paragon levels: XP per level and the account's Paragon XP.

Past level 70 every run's XP goes to Paragon, and the game log names it in each run commit
("xp=332590 (level 70)"). There is no running total in the log, so the tracker adds up the XP of
all level-70 runs it has seen (Game.log and Game-prev.log, each run once by its run id) and keeps
the total in its config. From the total and the XP table follow the Paragon level and the progress.

The history is complete when the log still shows the run that reached level 70. Otherwise the
Paragon level shown in the game's HUD ("Lv. 70 (3)") sets a lower bound; the progress inside that
level is then unknown and the figures are marked as estimates.

XP per level (XP to reach the next one) from the wiki's fixed points; levels in between are
interpolated linearly, past 1000 every level takes 10,175,200 XP more.
"""
POINTS = [(1, 44_289_770), (2, 44_917_030), (5, 46_799_600), (10, 49_937_480), (20, 145_378_170),
          (30, 160_695_480), (40, 176_012_790), (45, 183_671_840), (50, 191_330_890), (100, 191_330_890),
          (200, 191_330_890), (250, 320_174_360), (300, 449_017_830), (400, 615_599_600),
          (475, 1_378_750_660), (500, 1_633_130_660), (600, 2_650_650_660), (1000, 6_720_730_660)]
PAST_LAST = 10_175_200
MAX_SEEN = 5_000  # run ids kept to count every run once (more than two logs ever hold)
EXACT = {p for p, _ in POINTS}


def xp_to_next(level: int) -> int:
    """XP Paragon `level` takes to reach level + 1."""
    level = max(int(level), 1)
    for (p0, x0), (p1, x1) in zip(POINTS, POINTS[1:]):
        if p0 <= level <= p1:
            return int(round(x0 + (x1 - x0) * (level - p0) / (p1 - p0)))
    p, x = POINTS[-1]
    return x + (level - p) * PAST_LAST


def is_table_exact(level: int) -> bool:
    """True when the wiki lists this level itself (or a flat stretch), not an interpolation."""
    return level in EXACT or 50 <= level <= 200 or level >= 475


def split(total: int) -> tuple:
    """(Paragon level, XP inside it) for a total of Paragon XP; Paragon starts at level 1."""
    level, xp = 1, max(int(total), 0)
    while xp >= xp_to_next(level):
        xp -= xp_to_next(level)
        level += 1
    return level, xp


def level_start(level: int) -> int:
    """Total Paragon XP at the start of `level`."""
    return sum(xp_to_next(p) for p in range(1, max(int(level), 1)))


class Paragon:
    """Total Paragon XP of the account (shared by all characters)."""

    def __init__(self, saved: dict | None = None):
        saved = saved or {}
        self.total = saved.get("total")
        self.complete = bool(saved.get("complete"))   # the run that reached level 70 was counted
        self.hud_level = saved.get("hud_level")
        self.seen_list = list(saved.get("seen") or [])[-MAX_SEEN:]
        self.seen = set(self.seen_list)
        self.from_bar = bool(saved.get("from_bar"))   # total taken from the HUD bar (history incomplete)
        self.changed = False

    def to_dict(self) -> dict:
        return {"total": self.total, "complete": self.complete, "hud_level": self.hud_level,
                "seen": self.seen_list[-MAX_SEEN:], "from_bar": self.from_bar}

    def known(self) -> bool:
        return self.total is not None

    @property
    def level(self):
        return split(self.total)[0] if self.total is not None else self.hud_level

    @property
    def xp(self):
        return split(self.total)[1] if self.total is not None else None

    def reached_70(self):
        """The log shows the run that reached level 70: everything after it is Paragon XP.
        An estimated total (started from the HUD level) is counted again from here."""
        if not self.complete:
            self.total, self.complete, self.changed = 0, True, True
            self.seen, self.seen_list = set(), []

    def add(self, run_id: str, xp: int) -> bool:
        """XP of a run committed at level 70 (by a character that was 70 before the run)."""
        if not run_id or run_id in self.seen:
            return False
        self.seen.add(run_id)
        self.seen_list.append(run_id)
        if len(self.seen_list) > MAX_SEEN * 1.2:
            self.seen_list = self.seen_list[-MAX_SEEN:]
            self.seen = set(self.seen_list)
        self.total = (self.total or 0) + xp
        self.changed = True
        return True

    def observe_hud(self, level: int, fill: float | None = None):
        """Paragon level (and bar fill 0..1 at level 70) read from the game's HUD.
        A history that is not complete (the run that reached 70 is not in the logs - e.g. the tracker was
        installed later) takes its total from the HUD: start of the level plus the bar's share of it. Runs
        counted afterwards add to it; the bar keeps correcting it while it is the only source."""
        if level < 1:
            return
        if fill is not None and not self.complete and 0.0 <= fill <= 1.0:
            est = level_start(level) + int(fill * xp_to_next(level))
            cur_level = split(self.total)[0] if self.total is not None else None
            # small bar steps (separators, a run not committed yet) are noise; a different level or a clear
            # difference is taken
            if self.total is None or cur_level != level or abs(est - self.total) > 0.03 * xp_to_next(level):
                self.total, self.hud_level, self.from_bar, self.changed = est, level, True, True
                return
        if level == self.hud_level and (self.total is None or split(self.total)[0] >= level):
            return
        self.hud_level = level
        if self.total is None or split(self.total)[0] < level:
            short = level_start(level) - (self.total or 0)
            # the HUD can show the new level a moment before its run commit is in the log: a counted
            # history that is only a little behind stays complete
            if not (self.complete and self.total is not None and short <= 0.05 * xp_to_next(level - 1)):
                self.complete = False
            self.total = level_start(level)
        self.changed = True

    def exact(self) -> bool:
        return self.complete and self.total is not None and is_table_exact(split(self.total)[0])
