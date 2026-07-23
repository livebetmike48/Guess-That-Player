"""
Single-play clip source for the Guess game -- July 22 rework.

Problem it solves: the old source pulled generic highlight PACKAGES
(recaps, montages) and sliced 8 seconds out blind, so clips sometimes
showed ads, mound walks, or broadcast filler. No slicing heuristic can
fix that, because the play's position inside a package is unknowable.

This module picks a real individual PLAY instead -- a home run for
batter mode, a strikeout for pitcher mode -- and resolves that play's
own clip through the Film Room pipeline proven in the Errors bot. A
single-play clip IS the play: starts at the windup, no ads, no filler,
in-play by construction. Coverage lesson from the Errors bot applies in
our favor: Film Room clips highlight-tier plays fast and reliably, and
HRs/Ks are exactly that tier (we also only ever ask for YESTERDAY's
plays, so clips have had all night to process).

Returns the same contract as mlb_api.pick_daily_highlight
({mp4_url, player_id, player_name, title}), so bot.py can try this
first and fall back to the old source if nothing resolves.
"""
import re
import random
import logging

import requests

log = logging.getLogger("guess_game.plays")

MLB_BASE = "https://statsapi.mlb.com/api/v1"
MLB_BASE_V1_1 = "https://statsapi.mlb.com/api/v1.1"

ANY_MP4_RE = re.compile(r'https://[^"\'\\\s>]+\.mp4')
FILMROOM_GQL = (
    "https://fastball-gateway.mlb.com/graphql"
    '?query={mediaPlayback(ids:["%s"],languagePreference:EN,idType:PLAY_ID)'
    "{feeds{playbacks{name url}}}}"
)

# Bound the work per pick: how many candidate plays we try to resolve a
# clip for before giving up and letting bot.py fall back to the old source.
MAX_RESOLVE_ATTEMPTS = 12

BATTER_EVENT_TYPES = {"home_run"}
PITCHER_EVENT_TYPES = {"strikeout", "strikeout_double_play"}


def _final_game_pks(date_str: str) -> list[int]:
    resp = requests.get(
        f"{MLB_BASE}/schedule", params={"sportId": 1, "date": date_str}, timeout=15,
    )
    resp.raise_for_status()
    pks = []
    for date_entry in resp.json().get("dates", []):
        for g in date_entry.get("games", []):
            if g.get("status", {}).get("abstractGameState") == "Final":
                pks.append(g["gamePk"])
    return pks


def _collect_candidates(game_pk: int, want_pitcher: bool) -> list[dict]:
    """All qualifying plays in one game: HRs (batter mode) or Ks (pitcher
    mode), each with the responsible player and the play's LAST PITCH
    playId UUID -- the money pitch (the HR swing / the K pitch), and the
    Errors bot taught us trailing non-pitch events carry videoless
    playIds, so we specifically take the last isPitch event."""
    resp = requests.get(f"{MLB_BASE_V1_1}/game/{game_pk}/feed/live", timeout=20)
    resp.raise_for_status()
    data = resp.json()

    wanted_types = PITCHER_EVENT_TYPES if want_pitcher else BATTER_EVENT_TYPES
    candidates = []
    for play in (data.get("liveData") or {}).get("plays", {}).get("allPlays", []):
        event_type = (play.get("result") or {}).get("eventType")
        if event_type not in wanted_types:
            continue
        matchup = play.get("matchup") or {}
        person = matchup.get("pitcher") if want_pitcher else matchup.get("batter")
        if not person or not person.get("id"):
            continue

        play_uuid = None
        for event in reversed(play.get("playEvents", [])):
            if event.get("isPitch") and event.get("playId"):
                play_uuid = event["playId"]
                break
        if not play_uuid:
            continue

        candidates.append({
            "player_id": person["id"],
            "player_name": person.get("fullName", "Unknown"),
            "title": (play.get("result") or {}).get("description", ""),
            "play_uuid": play_uuid,
        })
    return candidates


def _resolve_clip(play_uuid: str) -> str | None:
    """The Film Room chain proven in the Errors bot (Savant page skipped --
    confirmed dead JS shell): direct mediaPlayback lookup first, then the
    SEARCH index, which was observed covering plays the direct lookup
    missed within the same game."""
    try:
        resp = requests.get(
            FILMROOM_GQL % play_uuid, timeout=15,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        if resp.status_code == 200 and resp.text:
            m = ANY_MP4_RE.search(resp.text)
            if m:
                log.info("Clip via Film Room direct for %s", play_uuid)
                return m.group(0)
    except Exception as e:
        log.warning("Film Room direct failed for %s: %s", play_uuid, e)

    try:
        gql = {
            "operationName": "Search",
            "variables": {
                "queryType": "STRUCTURED",
                "query": f'PlayId = "{play_uuid}" Order By Timestamp DESC',
                "limit": 5, "page": 0,
                "languagePreference": "EN",
                "contentPreference": "CMS_FIRST",
            },
            "query": (
                "query Search($query: String!, $page: Int, $limit: Int, "
                "$queryType: QueryType, $contentPreference: ContentPreference, "
                "$languagePreference: LanguagePreference) { "
                "search(query: $query, page: $page, limit: $limit, "
                "queryType: $queryType, contentPreference: $contentPreference, "
                "languagePreference: $languagePreference) { "
                "plays { mediaPlayback { slug feeds { type playbacks { name url } } } } } }"
            ),
        }
        resp = requests.post(
            "https://fastball-gateway.mlb.com/graphql", json=gql, timeout=15,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json",
                     "Content-Type": "application/json"},
        )
        if resp.status_code == 200 and resp.text:
            m = ANY_MP4_RE.search(resp.text)
            if m:
                log.info("Clip via Film Room SEARCH for %s", play_uuid)
                return m.group(0)
    except Exception as e:
        log.warning("Film Room SEARCH failed for %s: %s", play_uuid, e)

    return None


def pick_play(date_str: str, want_pitcher: bool) -> dict | None:
    """Picks a random qualifying play from date_str's games that has a
    resolvable single-play clip. Same return contract as
    mlb_api.pick_daily_highlight; None means the caller should fall back."""
    try:
        game_pks = _final_game_pks(date_str)
    except Exception as e:
        log.error("Schedule fetch failed for %s: %s", date_str, e)
        return None
    if not game_pks:
        log.warning("No final games on %s", date_str)
        return None

    random.shuffle(game_pks)
    candidates: list[dict] = []
    for pk in game_pks:
        try:
            candidates.extend(_collect_candidates(pk, want_pitcher))
        except Exception as e:
            log.warning("Feed fetch failed for game %s: %s", pk, e)
        if len(candidates) >= 40:
            break  # plenty of pool; stop burning feed requests

    if not candidates:
        log.warning("No %s plays found on %s", "K" if want_pitcher else "HR", date_str)
        return None

    random.shuffle(candidates)
    for candidate in candidates[:MAX_RESOLVE_ATTEMPTS]:
        mp4_url = _resolve_clip(candidate["play_uuid"])
        if mp4_url:
            log.info("Picked %s play: %s", "K" if want_pitcher else "HR", candidate["player_name"])
            return {
                "mp4_url": mp4_url,
                "player_id": candidate["player_id"],
                "player_name": candidate["player_name"],
                "title": candidate["title"],
            }
    log.warning("No clip resolved after %d attempts (%s mode, %s)",
                min(len(candidates), MAX_RESOLVE_ATTEMPTS),
                "pitcher" if want_pitcher else "batter", date_str)
    return None
