#!/usr/bin/env python3
"""Netflix-Katalog DE/HU: holt Verfügbarkeit (TMDB/JustWatch) und Ratings (OMDb).
Filme werden nie gelöscht, nur als nicht verfügbar markiert."""
import json, os, sys, time, urllib.parse, urllib.request
from datetime import date, datetime, timedelta

TMDB_KEY = os.environ["TMDB_API_KEY"]
OMDB_KEY = os.environ.get("OMDB_API_KEY", "")
OMDB_LIMIT = int(os.environ.get("OMDB_LIMIT", "950"))   # Free-Tier: 1000/Tag
RATING_MAX_AGE_DAYS = 45
REGIONS = ["DE", "HU"]
NETFLIX_ID = 8
DB_FILE = "data/movies.json"
TODAY = date.today().isoformat()


def get_json(url, retries=3):
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            if i == retries - 1:
                raise
            print(f"  Fehler ({e}), neuer Versuch …")
            time.sleep(3 * (i + 1))


def tmdb(path, **params):
    params["api_key"] = TMDB_KEY
    return get_json(f"https://api.themoviedb.org/3{path}?{urllib.parse.urlencode(params)}")


def fetch_region(region):
    """Alle Netflix-Filme einer Region. Gesplittet nach Jahrzehnten (TMDB max. 500 Seiten)."""
    found = {}
    ranges = [(None, "1969-12-31")] + [(f"{y}-01-01", f"{y+9}-12-31") for y in range(1970, 2030, 10)] + [("2030-01-01", None)]
    for start, end in ranges:
        page, total = 1, 1
        while page <= min(total, 500):
            p = dict(watch_region=region, with_watch_providers=NETFLIX_ID,
                     with_watch_monetization_types="flatrate", language="de-DE",
                     sort_by="popularity.desc", page=page)
            if start: p["primary_release_date.gte"] = start
            if end: p["primary_release_date.lte"] = end
            d = tmdb("/discover/movie", **p)
            total = d.get("total_pages", 0)
            for m in d.get("results", []):
                found[str(m["id"])] = m
            page += 1
            time.sleep(0.05)
    return found


def omdb(imdb_id):
    d = get_json(f"https://www.omdbapi.com/?i={imdb_id}&apikey={OMDB_KEY}")
    if d.get("Response") == "False":
        if "limit" in d.get("Error", "").lower():
            raise RuntimeError("OMDb-Tageslimit erreicht")
        return {}
    r = {"imdb": None, "rt": None, "mc": None, "votes": None}
    try: r["imdb"] = float(d.get("imdbRating"))
    except (TypeError, ValueError): pass
    try: r["votes"] = int(d.get("imdbVotes", "").replace(",", ""))
    except ValueError: pass
    for s in d.get("Ratings", []):
        if s["Source"] == "Rotten Tomatoes":
            r["rt"] = int(s["Value"].rstrip("%"))
        elif s["Source"] == "Metacritic":
            r["mc"] = int(s["Value"].split("/")[0])
    return r


def main():
    os.makedirs("data", exist_ok=True)
    db = {"movies": {}}
    if os.path.exists(DB_FILE):
        with open(DB_FILE, encoding="utf-8") as f:
            db = json.load(f)
    movies = db["movies"]

    # 1) Verfügbarkeit
    for region in REGIONS:
        print(f"Lade Netflix {region} …")
        current = fetch_region(region)
        prev = sum(1 for m in movies.values() if m["avail"].get(region, {}).get("on"))
        print(f"  {len(current)} Filme gefunden (vorher verfügbar: {prev})")
        # Schutz: bei API-Ausfall nicht alles ausgrauen
        safe = len(current) >= 0.5 * prev
        if not safe:
            print("  WARNUNG: Ergebnis zu klein, Ausgrauen wird übersprungen.")
        for mid, m in current.items():
            e = movies.setdefault(mid, {
                "id": int(mid), "title": m.get("title"), "orig": m.get("original_title"),
                "year": (m.get("release_date") or "")[:4] or None,
                "poster": m.get("poster_path"), "added": TODAY, "avail": {}, "r": None,
            })
            e["title"] = m.get("title") or e["title"]
            e["poster"] = m.get("poster_path") or e.get("poster")
            e["genres"] = m.get("genre_ids") or e.get("genres") or []
            a = e["avail"].setdefault(region, {"first": TODAY})
            a.update(on=True, last=TODAY)
        if safe:
            for mid, e in movies.items():
                a = e["avail"].get(region)
                if a and a.get("on") and mid not in current:
                    a["on"] = False
                    a["gone"] = TODAY

    # 2) Ungarischer Titel + IMDb-ID für neue Filme (ein Aufruf pro Film)
    missing = [e for e in movies.values() if "imdb_id" not in e or "title_hu" not in e or "runtime" not in e]
    print(f"Hole HU-Titel und IMDb-IDs für {len(missing)} Filme …")
    for e in missing:
        try:
            d = tmdb(f"/movie/{e['id']}", language="hu-HU", append_to_response="external_ids")
            e["title_hu"] = d.get("title") or e.get("orig")
            e["runtime"] = d.get("runtime") or None
            e["imdb_id"] = (d.get("external_ids") or {}).get("imdb_id") or d.get("imdb_id")
        except Exception as ex:
            print(f"  {e['id']}: {ex}")
        time.sleep(0.03)

    # 3) Ratings: zuerst Filme ohne Rating, dann veraltete (nur aktuell verfügbare)
    if OMDB_KEY:
        cutoff = (datetime.now() - timedelta(days=RATING_MAX_AGE_DAYS)).date().isoformat()
        is_on = lambda e: any(a.get("on") for a in e["avail"].values())
        todo = [e for e in movies.values() if e.get("imdb_id") and not e.get("r_date")]
        todo += sorted([e for e in movies.values() if e.get("imdb_id") and e.get("r_date")
                        and e["r_date"] < cutoff and is_on(e)], key=lambda e: e["r_date"])
        print(f"Ratings offen: {len(todo)}, heute max. {OMDB_LIMIT}")
        for e in todo[:OMDB_LIMIT]:
            try:
                e["r"] = omdb(e["imdb_id"])
                e["r_date"] = TODAY
            except RuntimeError as ex:
                print(f"  {ex}"); break
            except Exception as ex:
                print(f"  {e['imdb_id']}: {ex}")
            time.sleep(0.05)

    db["updated"] = datetime.now().isoformat(timespec="minutes")
    db["regions"] = REGIONS
    # Genre-Namen DE/HU
    try:
        names = {}
        for lang, code in (("de", "de-DE"), ("hu", "hu-HU")):
            for g in tmdb("/genre/movie/list", language=code).get("genres", []):
                names.setdefault(str(g["id"]), {})[lang] = g["name"]
        db["genres"] = names
    except Exception as ex:
        print(f"Genres: {ex}")
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Fertig: {len(movies)} Filme in der Datenbank.")


if __name__ == "__main__":
    sys.exit(main())
