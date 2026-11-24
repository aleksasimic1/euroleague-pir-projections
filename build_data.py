"""Gradi data.json za index.html iz EuroLeague API-ja.

Pokretanje:  pip install euroleague-api pandas requests
             python build_data.py

Raspored (sljedeći protivnik) se traži ovim redom:
  1. fixtures.json (ako postoji) - ručni unos, uvijek pobjeđuje
  2. API v1 /schedules (XML)
  3. API v2 /games (JSON)
Format fixtures.json:
  [{"round": 12, "home": "BAR", "away": "RMB"}, ...]
(imena/kodovi tima moraju odgovarati onome što skripta ispiše kao "Timovi u box scoreu")
"""
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

import pandas as pd
import requests

SEASON = 2026          # sezona 2026-27 = E2026
COMP = "E"             # E = EuroLeague, U = EuroCup
API = "https://api-live.euroleague.net"
HEADERS = {"User-Agent": "Mozilla/5.0 (fantasy-pir-predictor)"}


# ---------------------------------------------------------------- pomoćne
def pick(df, *candidates):
    for c in candidates:
        if c in df.columns:
            return c
    sys.exit(f"Nema nijedne od kolona {candidates}. Dostupne: {list(df.columns)}")


def minutes(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v)
    if ":" in s:
        m, sec = s.split(":")
        return int(m) + int(sec) / 60
    try:
        return float(s)
    except ValueError:
        return 0.0


def norm(s):
    return re.sub(r"\W+", "", str(s)).lower()


def to_int(v):
    m = re.search(r"\d+", str(v))
    return int(m.group()) if m else None


# ---------------------------------------------------------------- raspored
def xml_text(el, *names):
    for ch in el:
        if ch.tag.split("}")[-1].lower() in names:
            return (ch.text or "").strip()
    return ""


def games_manual(path="fixtures.json"):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    return [{
        "round": int(r["round"]), "gamecode": "", "played": False,
        "home": [r["home"], r["home"]], "away": [r["away"], r["away"]],
    } for r in rows]


def games_v1():
    r = requests.get(f"{API}/v1/schedules", params={"seasoncode": f"{COMP}{SEASON}"},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for item in root.iter():
        if item.tag.split("}")[-1].lower() != "item":
            continue
        rnd = to_int(xml_text(item, "round"))
        if rnd is None:
            continue
        played = xml_text(item, "played")
        out.append({
            "round": rnd,
            "gamecode": xml_text(item, "gamecode"),
            "played": (played.lower() == "true") if played else None,
            "home": [xml_text(item, "homecode"), xml_text(item, "hometeam")],
            "away": [xml_text(item, "awaycode"), xml_text(item, "awayteam")],
        })
    return out


def games_v2():
    r = requests.get(f"{API}/v2/competitions/{COMP}/seasons/{COMP}{SEASON}/games",
                     params={"limit": 500}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    j = r.json()
    items = j.get("data", []) if isinstance(j, dict) else j
    if items:
        print("v2 primjer polja:", list(items[0].keys()))

    def team(t):
        t = t or {}
        club = t.get("club", t) or {}
        return [club.get("code") or club.get("tvCode") or "", club.get("name") or ""]

    out = []
    for g in items:
        rnd = to_int(g.get("round"))
        if rnd is None:
            continue
        out.append({
            "round": rnd,
            "gamecode": str(g.get("gameCode") or g.get("code") or ""),
            "played": g.get("played"),
            "home": team(g.get("local") or g.get("home")),
            "away": team(g.get("road") or g.get("away")),
        })
    return out


def load_schedule():
    for name, fn in (("fixtures.json", games_manual), ("API v1", games_v1), ("API v2", games_v2)):
        try:
            games = fn()
            print(f"Raspored [{name}]: {len(games)} utakmica")
            if games:
                return games
        except Exception as e:
            print(f"Raspored [{name}] nije uspio: {type(e).__name__}: {e}")
    return []


def next_fixtures(games, box_teams, played_codes):
    """Vrati (next_round, {box_tim: {'opp': box_tim_protivnika, 'label': ..., 'home': 0/1}})."""
    norm_box = {norm(t): t for t in box_teams}

    def resolve(pair):
        for v in pair:
            if v and norm(v) in norm_box:
                return norm_box[norm(v)]
        return None

    def is_played(g):
        if g["gamecode"] and g["gamecode"] in played_codes:
            return True
        return bool(g["played"]) if g["played"] is not None else False

    played_rounds = [g["round"] for g in games if is_played(g)]
    unplayed = [g for g in games if not is_played(g)]
    if not unplayed:
        return None, {}

    max_played = max(played_rounds) if played_rounds else 0
    later = [g["round"] for g in unplayed if g["round"] > max_played]
    next_round = min(later) if later else min(g["round"] for g in unplayed)

    fixtures, unresolved = {}, set()
    for g in unplayed:
        if g["round"] != next_round:
            continue
        h, a = resolve(g["home"]), resolve(g["away"])
        for side, pair in (("home", g["home"]), ("away", g["away"])):
            if resolve(pair) is None:
                unresolved.add(pair[0] or pair[1])
        if h and a:
            fixtures[h] = {"opp": a, "label": g["away"][0] or g["away"][1], "home": 1}
            fixtures[a] = {"opp": h, "label": g["home"][0] or g["home"][1], "home": 0}

    if unresolved:
        print("UPOZORENJE: ovi timovi iz rasporeda se ne poklapaju s box scoreom:", sorted(unresolved))
        print("Timovi u box scoreu:", sorted(box_teams))
        print("Rješenje: napravi fixtures.json s imenima iz 'Timovi u box scoreu'.")
    return next_round, fixtures


# ---------------------------------------------------------------- main
def main():
    from euroleague_api.boxscore_data import BoxScoreData

    box = BoxScoreData(COMP).get_players_boxscore_stats_single_season(SEASON)
    print("Kolone:", list(box.columns))

    c_name = pick(box, "Player", "player")
    c_team = pick(box, "Team", "team")
    c_min = pick(box, "Minutes", "minutes")
    c_pir = pick(box, "Valuation", "valuation", "PIR")
    c_home = pick(box, "Home", "home")
    c_round = pick(box, "Round", "round")
    c_game = pick(box, "Gamecode", "gamecode", "GameCode")

    box = box[~box[c_name].astype(str).str.contains("Total|Team", case=False, na=False)].copy()
    box["_min"] = box[c_min].apply(minutes)
    box = box[box["_min"] > 0].sort_values([c_round, c_game])

    teams_by_game = box.groupby(c_game)[c_team].apply(lambda s: list(dict.fromkeys(s)))

    def opp_of(game, team):
        others = [t for t in teams_by_game.get(game, []) if t != team]
        return str(others[0]) if others else ""

    box_teams = set(box[c_team].astype(str))
    played_codes = set(box[c_game].astype(str))
    next_round, fixtures = next_fixtures(load_schedule(), box_teams, played_codes)
    print(f"next_round={next_round}, timova s poznatim protivnikom: {len(fixtures)} / {len(box_teams)}")

    players = []
    for (name, team), g in box.groupby([c_name, c_team]):
        fx = fixtures.get(str(team))
        players.append({
            "name": str(name).title() if str(name).isupper() else str(name),
            "team": str(team),
            "next_opp": (("vs " if fx["home"] else "@ ") + fx["label"]) if fx else "",
            "next_home": fx["home"] if fx else None,
            "next_opp_team": fx["opp"] if fx else "",
            "games": [
                {
                    "pir": float(row[c_pir]),
                    "min": float(row["_min"]),
                    "home": 1 if bool(row[c_home]) else 0,
                    "round": int(row[c_round]),
                    "opp": opp_of(row[c_game], team),
                }
                for _, row in g.iterrows()
            ],
        })

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump({"next_round": next_round, "players": players}, f, ensure_ascii=False)
    print(f"Upisano {len(players)} igrača")


if __name__ == "__main__":
    main()