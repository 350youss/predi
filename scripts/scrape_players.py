#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scrape l'effectif complet des 18 clubs de Ligue 1 (Transfermarkt), PRO + les
équipes B / U19 / U17 rattachées -> L1/data/players.json

Base de données "vrais joueurs de vrais clubs" pour Predis Indiv : on ne
peut choisir qu'un joueur réellement licencié au club au moment du dernier
scrape, jamais de saisie libre. Les équipes B/U19/U17 alimentent notamment
la catégorie "Révélation", qui doit pouvoir piocher au-delà du seul groupe
professionnel. Auto-exécuté par une GitHub Action planifiée (voir
.github/workflows/update-players.yml) : ne dépend d'aucune machine locale
allumée.

Best-effort : si un club échoue ce passage (site indisponible, changement
de structure), son ancien effectif est conservé plutôt que vidé.

Usage : python scripts/scrape_players.py
"""
import os, re, sys, json, time
from datetime import datetime
import requests
from bs4 import BeautifulSoup

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "scripts", ".cache")
OUT   = os.path.join(ROOT, "L1", "data", "players.json")
os.makedirs(CACHE, exist_ok=True)
os.makedirs(os.path.dirname(OUT), exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# id (utilisé partout ailleurs sur predi/L1), nom, slug transfermarkt, id transfermarkt
CLUBS = [
    ("psg",   "Paris Saint-Germain",  "fc-paris-saint-germain", 583),
    ("rcl",   "RC Lens",              "rc-lens",                826),
    ("losc",  "LOSC Lille",           "losc-lille",             1082),
    ("ol",    "Olympique Lyonnais",   "olympique-lyon",         1041),
    ("om",    "Olympique de Marseille","olympique-marseille",   244),
    ("srfc",  "Stade Rennais FC",     "fc-stade-rennes",        273),
    ("asm",   "AS Monaco",            "as-monaco",              162),
    ("rcsa",  "RC Strasbourg",        "rc-strassburg-alsace",   667),
    ("fcl",   "FC Lorient",           "fc-lorient",             1158),
    ("tfc",   "Toulouse FC",          "fc-toulouse",            415),
    ("pfc",   "Paris FC",             "paris-fc",               10004),
    ("sb29",  "Stade Brestois 29",    "stade-brest-29",         3911),
    ("sco",   "Angers SCO",           "sco-angers",             1420),
    ("hac",   "Le Havre AC",          "ac-le-havre",            738),
    ("aja",   "AJ Auxerre",           "aj-auxerre",             290),
    ("ogcn",  "OGC Nice",             "ogc-nizza",              417),
    ("estac", "ESTAC Troyes",         "es-troyes-ac",           1095),
    ("lm",    "Le Mans FC",           "le-mans-fc",             1164),
]

SUB_SQUADS = ["B", "U19", "U17"]

POS = {
    "Gardien de but": "GdB", "Arrière droit": "ArD", "Arrière gauche": "ArG",
    "Défenseur central": "DC", "Milieu défensif": "MDC", "Milieu central": "MC",
    "Milieu offensif": "MO", "Milieu droit": "MD", "Milieu gauche": "MG",
    "Ailier droit": "AiD", "Ailier gauche": "AiG", "Avant-centre": "AC",
    "Second attaquant": "SA", "Attaquant": "AC", "Défenseur": "DC", "Milieu": "MC",
}
def pos_short(full):
    full = (full or "").strip()
    if full in POS: return POS[full]
    return "".join(w[0] for w in full.split()[:3]).upper()[:4] or "?"

GRP = {
    "Gardien de but": "GK",
    "Arrière droit": "DEF", "Arrière gauche": "DEF", "Défenseur central": "DEF", "Défenseur": "DEF",
    "Milieu défensif": "MID", "Milieu central": "MID", "Milieu offensif": "MID",
    "Milieu droit": "MID", "Milieu gauche": "MID", "Milieu": "MID",
    "Ailier droit": "ATT", "Ailier gauche": "ATT", "Avant-centre": "ATT",
    "Second attaquant": "ATT", "Attaquant": "ATT",
}
def pos_group(full):
    return GRP.get((full or "").strip(), "MID")


def fetch_url(fn, url, label=""):
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            if r.status_code == 200 and len(r.content) > 5000:
                open(fn, "wb").write(r.content)
                return r.content
        except Exception as e:
            print(f"  ! {label or url} : {e} (essai {attempt+1})")
        time.sleep(2 + attempt * 2)
    if os.path.exists(fn):
        print(f"  -> cache utilisé pour {label or url}")
        return open(fn, "rb").read()
    return None


def find_sub_squads(raw):
    """repère les liens vers les équipes B / U19 / U17 rattachées, présents
       dans la page principale ('startseite') du club."""
    soup = BeautifulSoup(raw, "lxml")
    found = {}
    for a in soup.find_all("a", href=re.compile(r"/startseite/verein/\d+")):
        txt = a.get_text(strip=True)
        m = re.search(r"/([a-z0-9-]+)/startseite/verein/(\d+)", a["href"])
        if not m:
            continue
        slug, cid = m.group(1), int(m.group(2))
        for suf in SUB_SQUADS:
            if re.search(rf"\b{suf}\b", txt) and suf not in found:
                found[suf] = (slug, cid)
    return found


def parse_squad(raw, squad_tag):
    soup = BeautifulSoup(raw, "lxml")
    table = soup.select_one("table.items")
    if not table or not table.find("tbody"):
        return []
    players = []
    for tr in table.find("tbody").find_all("tr", recursive=False):
        td = tr.find("td", class_="posrela")
        if not td:
            continue
        namea = td.select_one(".hauptlink a")
        if not namea:
            continue
        name = (namea.get("title") or namea.get_text(strip=True)).strip()
        inner_rows = td.find_all("tr")
        pos_full = inner_rows[-1].get_text(" ", strip=True) if inner_rows else ""
        players.append({
            "name": name, "pos": pos_short(pos_full), "posGroup": pos_group(pos_full), "squad": squad_tag,
        })
    return players


def fetch_coach(slug, tmid):
    """entraîneur principal actuel, depuis la page effectif technique du
       club (première personne de la section 'Équipe d'entraîneurs')."""
    fn = os.path.join(CACHE, f"staff_{tmid}.html")
    raw = fetch_url(fn, f"https://www.transfermarkt.fr/{slug}/mitarbeiter/verein/{tmid}", f"staff {slug}")
    if not raw:
        return None
    soup = BeautifulSoup(raw, "lxml")
    h2 = soup.find("h2", class_="content-box-headline", string=re.compile(r"entra.neur", re.I))
    if not h2:
        return None
    box = h2.find_parent("div", class_="box")
    if not box:
        return None
    a = box.select_one("table.inline-table .hauptlink a")
    return a.get_text(strip=True) if a else None


def scrape_club(cid, name, slug, tmid):
    players = []

    fn_squad = os.path.join(CACHE, f"{cid}.html")
    raw_squad = fetch_url(fn_squad, f"https://www.transfermarkt.fr/{slug}/kader/verein/{tmid}/plus/1", name)
    if raw_squad:
        players += parse_squad(raw_squad, "PRO")

    fn_start = os.path.join(CACHE, f"start_{cid}.html")
    raw_start = fetch_url(fn_start, f"https://www.transfermarkt.fr/{slug}/startseite/verein/{tmid}", f"{name} (page club)")
    if raw_start:
        subs = find_sub_squads(raw_start)
        for suf in SUB_SQUADS:
            if suf not in subs:
                continue
            sub_slug, sub_tmid = subs[suf]
            fn_sub = os.path.join(CACHE, f"{cid}_{suf}.html")
            raw_sub = fetch_url(fn_sub, f"https://www.transfermarkt.fr/{sub_slug}/kader/verein/{sub_tmid}/plus/1", f"{name} {suf}")
            if raw_sub:
                sub_players = parse_squad(raw_sub, suf)
                print(f"    · {name} {suf} -> {len(sub_players)} joueurs")
                players += sub_players

    return players


def main():
    prev = {}
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding="utf-8"))
            prev = {c["id"]: c for c in old.get("clubs", [])}
        except Exception:
            pass

    clubs_data = []
    ok_count = 0
    for cid, name, slug, tmid in CLUBS:
        print(f"· {name} …")
        players = scrape_club(cid, name, slug, tmid)
        coach = fetch_coach(slug, tmid)
        pro_n = sum(1 for p in players if p["squad"] == "PRO")
        if players:
            ok_count += 1
            print(f"  -> {pro_n} pro + {len(players)-pro_n} jeunes/réserve = {len(players)} au total, entraîneur : {coach or '?'}")
        else:
            print(f"  !! échec {name}, effectif précédent conservé si dispo")
            if cid in prev and prev[cid].get("players"):
                players = prev[cid]["players"]
        if not coach and cid in prev:
            coach = prev[cid].get("coach")
        clubs_data.append({"id": cid, "name": name, "players": players, "coach": coach})

    total = sum(len(c["players"]) for c in clubs_data)
    out = {
        "season": "2026-27",
        "updated": datetime.now().isoformat(timespec="seconds"),
        "clubs": clubs_data,
    }
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n{total} joueurs sur {len(clubs_data)} clubs -> {OUT} ({ok_count}/{len(CLUBS)} clubs scrapés avec succès ce passage)")


if __name__ == "__main__":
    main()
