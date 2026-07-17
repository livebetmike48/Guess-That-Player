"""
Finds candidate highlights for the guessing game. Tries yesterday's games
first (fresh content), then falls back to random dates across the whole
2026 season -- so off days, the All-Star break, and rainouts never leave
the game without a post, and the answer pool stays less predictable.

Clip types are now locked per mode:
  - batter game  -> HOME RUN clips only
  - pitcher game -> STRIKEOUT clips only
Detection is title-based (the editorial feed uses formulaic titles like
"Bazzana homers (12)" / "Skubal strikes out Alvarez"), same folded-text
approach as the existing last-name verification.
"""
import random
import unicodedata
from datetime import datetime, timedelta
import requests

BASE = "https://statsapi.mlb.com/api/v1"
SEASON_START = "2026-03-26"

# Compilation/recap videos feature many players -- the tagged player is
# often NOT the person most visible in a random 8-second slice. Skip them.
BAD_TITLE_WORDS = ["highlights", "recap", "condensed", "top plays", "best of", "every "]

# Event detection by title. Folded/lowercased before matching.
# "homer" (substring) covers homers/homered/walk-off homer; grand slams
# often omit the word "homer" so they're listed explicitly.
HR_TITLE_WORDS = ["homer", "home run", "grand slam", "goes deep"]
# "Skubal strikes out Alvarez", "Skubal's 10th strikeout", "punches out".
K_TITLE_WORDS = ["strikes out", "strikeout", "punches out", "punchout"]


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def _is_hr_title(title_folded: str) -> bool:
    return any(w in title_folded for w in HR_TITLE_WORDS)


def _is_k_title(title_folded: str) -> bool:
    return any(w in title_folded for w in K_TITLE_WORDS)


def _title_matches_mode(title: str, want_pitcher: bool) -> bool:
    """Pitcher game only accepts strikeout clips; batter game only home
    run clips. A title matching BOTH (rare/weird) is rejected outright --
    can't trust which event the slice will show."""
    folded = _fold(title)
    if any(bad in folded for bad in BAD_TITLE_WORDS):
        return False
    hr, k = _is_hr_title(folded), _is_k_title(folded)
    if hr and k:
        return False
    return k if want_pitcher else hr


_NAME_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}


def _last_name_in_title(title_folded: str, player_name: str) -> bool:
    """The tagged player's LAST NAME must literally appear in the title --
    'Bazzana homers (12)' confirms Bazzana is the star of the clip. Fixes a
    real bug where a keyword tag was a different player than the one
    visibly featured, making everyone's correct guess score as wrong.
    Generational suffixes are stripped first: 'Bobby Witt Jr.' -> 'witt',
    since titles say 'Witt homers (21)' -- without this, suffix players
    could never be answers (long-standing silent exclusion, now fixed)."""
    parts = [p for p in _fold(player_name).split() if p not in _NAME_SUFFIXES]
    last_name = parts[-1] if parts else ""
    return bool(last_name) and last_name in title_folded


def get_final_game_pks(date_str: str) -> list[int]:
    resp = requests.get(
        f"{BASE}/schedule", params={"sportId": 1, "date": date_str}, timeout=15
    )
    resp.raise_for_status()
    pks = []
    for date_entry in resp.json().get("dates", []):
        for g in date_entry.get("games", []):
            if g.get("status", {}).get("abstractGameState") == "Final":
                pks.append(g["gamePk"])
    return pks


def get_highlights_with_players(game_pk: int, want_pitcher: bool) -> list[dict]:
    """Highlights from a game that have a playable mp4, a title matching
    the wanted event type (K for pitcher game / HR for batter game), and
    at least one tagged player verifiably named in the title.

    Strikeout titles usually name TWO players ("Skubal strikes out
    Alvarez") and the tag order isn't reliable, so this yields one
    candidate per name-verified tagged player; the caller's position
    check then picks the pitcher (or batter) among them."""
    resp = requests.get(f"{BASE}/game/{game_pk}/content", timeout=15)
    resp.raise_for_status()
    data = resp.json()

    items = (((data.get("highlights") or {}).get("highlights") or {}).get("items")) or []
    results = []
    for item in items:
        title = item.get("title", "")
        if not _title_matches_mode(title, want_pitcher):
            continue
        title_folded = _fold(title)

        tagged = []
        seen_ids = set()
        for kw in item.get("keywordsAll", []) or []:
            if kw.get("type") != "player_id":
                continue
            pid, pname = kw.get("value"), kw.get("displayName")
            if not pid or not pname or pid in seen_ids:
                continue
            seen_ids.add(pid)
            if _last_name_in_title(title_folded, pname):
                tagged.append((int(pid), pname))
        if not tagged:
            continue

        mp4_url = None
        for playback in item.get("playbacks", []) or []:
            url = playback.get("url", "")
            if url.endswith(".mp4"):
                mp4_url = url
                if "2500K" in url or "1800K" in url:
                    break
        if not mp4_url:
            continue

        for pid, pname in tagged:
            results.append({
                "player_id": pid,
                "player_name": pname,
                "title": title,
                "mp4_url": mp4_url,
            })
    return results


def get_player_position(player_id: int) -> str | None:
    """Position code: '1' = pitcher. Used to confirm the K clip candidate
    is the pitcher (not the punched-out batter) and vice versa."""
    resp = requests.get(f"{BASE}/people/{player_id}", timeout=15)
    resp.raise_for_status()
    people = resp.json().get("people", [])
    if not people:
        return None
    return (people[0].get("primaryPosition") or {}).get("code")


def _candidate_dates(yesterday: str, extra_random_days: int = 6) -> list[str]:
    """Yesterday first, then random dates spread across the season so far."""
    dates = [yesterday]
    start = datetime.strptime(SEASON_START, "%Y-%m-%d")
    end = datetime.strptime(yesterday, "%Y-%m-%d")
    span = (end - start).days
    if span > 0:
        offsets = random.sample(range(span), min(extra_random_days, span))
        dates += [(start + timedelta(days=o)).strftime("%Y-%m-%d") for o in offsets]
    return dates


def _pick_from_date(date_str: str, want_pitcher: bool) -> dict | None:
    game_pks = get_final_game_pks(date_str)
    random.shuffle(game_pks)
    for game_pk in game_pks:
        try:
            highlights = get_highlights_with_players(game_pk, want_pitcher)
        except Exception:
            continue
        random.shuffle(highlights)
        for h in highlights:
            try:
                position = get_player_position(h["player_id"])
            except Exception:
                continue
            if position is None:
                continue
            is_pitcher = position == "1"
            if is_pitcher == want_pitcher:
                h["is_pitcher"] = is_pitcher
                return h
    return None


def pick_daily_highlight(yesterday: str, want_pitcher: bool) -> dict | None:
    """Tries yesterday, then random season dates -- never stranded by an
    off day or the All-Star break. Pitcher game = strikeout clip credited
    to the pitcher; batter game = home run clip credited to the batter."""
    for date_str in _candidate_dates(yesterday):
        result = _pick_from_date(date_str, want_pitcher)
        if result is not None:
            return result
    return None
