"""Release notes of all versions: from GitHub (newest, includes versions not installed yet) or from
the CHANGELOG.md shipped with the tracker when GitHub cannot be reached."""
import json
import re
import urllib.request

import paths
from version import REPO, VERSION

API = f"https://api.github.com/repos/{REPO}/releases?per_page=100"


def online() -> list | None:
    """[{"tag", "date", "notes"}], newest first; None if GitHub is unreachable."""
    req = urllib.request.Request(API, headers={"User-Agent": f"DeskrawlTracker/{VERSION}",
                                               "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
    except Exception:
        return None
    return [{"tag": d.get("tag_name", ""), "date": (d.get("published_at") or "")[:10],
             "notes": (d.get("body") or "").replace("\r\n", "\n").strip()}
            for d in data if not d.get("draft") and not d.get("prerelease")]


def local() -> list:
    """Same list from CHANGELOG.md ("## v1.0.8 – 2026-10-06" starts a version)."""
    try:
        with open(paths.res("CHANGELOG.md"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    out = []
    for block in re.split(r"(?m)^## ", text)[1:]:
        head, _, body = block.partition("\n")
        m = re.match(r"(\S+)(?:\s+[–-]\s+(\S+))?", head.strip())
        if not m:
            continue
        # sections inside a version are "###" in the file, "##" in GitHub's notes
        notes = re.sub(r"(?m)^###", "##", body).strip()
        out.append({"tag": m.group(1), "date": m.group(2) or "", "notes": notes})
    return out
