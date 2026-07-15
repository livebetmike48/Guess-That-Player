"""
Finds candidate highlights for the guessing game from yesterday's completed
games. Uses the same content endpoint proven working in the Errors Bot all
week -- but this game is easier: we don't need a SPECIFIC play's clip, just
any highlight where we can confidently identify the featured player from
the highlight's own keyword metadata (player_id entries).
"""
import random
import requests

BASE = "https://statsapi.mlb.com/api/v1"


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


def get_highlights_with_players(game_pk: int) -> list[dict]:
    """Highlights from a game that have both a playable mp4 and a clearly
    identified featured player (first player_id keyword)."""
    resp = requests.get(f"{BASE}/game/{game_pk}/content", timeout=15)
    resp.raise_for_status()
    data = resp.json()

    items = (((data.get("highlights") or {}).get("highlights") or {}).get("items")) or []
    results = []
    for item in items:
        # Featured player from keyword metadata
        player_id = None
        player_name = None
        for kw in item.get("keywordsAll", []) or []:
            if kw.get("type") == "player_id":
                player_id = kw.get("value")
                player_name = kw.get("displayName")
                break
        if not player_id or not player_name:
            continue

        # Best mp4 playback URL (prefer medium quality -- big enough to see,
        # small enough to process and upload)
        mp4_url = None
        for playback in item.get("playbacks", []) or []:
            url = playback.get("url", "")
            if url.endswith(".mp4"):
                mp4_url = url
                if "2500K" in url or "1800K" in url:
                    break
        if not mp4_url:
            continue

        results.append({
            "player_id": int(player_id),
            "player_name": player_name,
            "title": item.get("title", ""),
            "mp4_url": mp4_url,
        })
    return results


def get_player_position(player_id: int) -> str | None:
    """Position code: '1' = pitcher. Used to route a highlight to the
    pitcher game or the batter game."""
    resp = requests.get(f"{BASE}/people/{player_id}", timeout=15)
    resp.raise_for_status()
    people = resp.json().get("people", [])
    if not people:
        return None
    return (people[0].get("primaryPosition") or {}).get("code")


def pick_daily_highlight(date_str: str, want_pitcher: bool) -> dict | None:
    """Picks a random suitable highlight from yesterday's games, where the
    featured player's role matches the requested game type. Shuffles games
    and items so the pick varies day to day."""
    game_pks = get_final_game_pks(date_str)
    random.shuffle(game_pks)

    for game_pk in game_pks:
        try:
            highlights = get_highlights_with_players(game_pk)
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
