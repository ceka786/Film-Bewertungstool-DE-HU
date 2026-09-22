#!/usr/bin/env python3
"""
NETFLIX-KATALÓGUS DE/HU – adatgyűjtő szkript
============================================
Mit csinál? Megkérdezi a TMDB-t, mely filmek futnak a német és a magyar
Netflixen, majd az OMDb-től elkéri az értékeléseket. Az eredményt egyetlen
fájlba írja: data/movies.json. Filmet SOSEM töröl, csak megjelöli, hogy
egy országban már nem elérhető.

Naponta egyszer fut a GitHub Actions által (lásd .github/workflows/update.yml).
Részletes magyarázat: DOKUMENTACIO.md
"""

# --- import: kész eszközök betöltése ---------------------------------------
# json      = adat szöveggé és vissza      | os   = környezeti változók, fájlok
# sys       = kilépési kód                 | time = várakozás két kérés között
# urllib.*  = internetes lekérés           | datetime = dátumszámítás
import json, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timedelta

# --- beállítások (konstansok) ----------------------------------------------
# A kulcsokat NEM ide írjuk! A GitHub Secrets-ből érkeznek környezeti változóként.
TMDB_KEY = os.environ["TMDB_API_KEY"]            # kötelező, enélkül leáll
OMDB_KEY = os.environ.get("OMDB_API_KEY", "")    # opcionális: enélkül nincs értékelés
OMDB_LIMIT = int(os.environ.get("OMDB_LIMIT", "950"))  # ingyenes keret: 1000/nap
RATING_MAX_AGE_DAYS = 45      # ennyi nap után frissítjük újra egy film értékelését
REGIONS = ["DE", "HU"]        # országkódok: Németország, Magyarország
NETFLIX_ID = 8                # a Netflix azonosítója a TMDB-nél
DB_FILE = "data/movies.json"  # az "adatbázisunk" – egyetlen JSON-fájl
TODAY = date.today().isoformat()  # mai dátum "2026-09-18" formában


# --- alapfüggvények ---------------------------------------------------------

def get_json(url, retries=3):
    """Letölt egy címet és JSON-ként adja vissza. Hiba esetén újrapróbálja.
    retries = hányszor próbálkozzon összesen; a szünet minden körben nő."""
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            if i == retries - 1:   # utolsó próbálkozás is elbukott -> hiba tovább
                raise
            print(f"  Hiba ({e}), újra …")
            time.sleep(3 * (i + 1))


def tmdb(path, **params):
    """TMDB-hívás. A kulcsot automatikusan hozzáteszi.
    Példa: tmdb("/movie/550", language="hu-HU")"""
    params["api_key"] = TMDB_KEY
    # urlencode: a paraméterekből "a=1&b=2" formájú szöveget csinál
    return get_json(f"https://api.themoviedb.org/3{path}?{urllib.parse.urlencode(params)}")


def fetch_region(region):
    """Egy ország ÖSSZES Netflix-filmje.

    Trükk: a TMDB egy kereséshez max. 500 oldalt ad vissza, ezért évtizedekre
    bontjuk a keresést, és minden évtizedet külön lapozunk végig.
    Visszaad: {"550": {film adatai}, ...} – kulcs a TMDB azonosító szövegként."""
    found = {}
    # dátumsávok: régi filmek, majd 1970-2029 évtizedenként, végül a jövő
    ranges = ([(None, "1969-12-31")]
              + [(f"{y}-01-01", f"{y+9}-12-31") for y in range(1970, 2030, 10)]
              + [("2030-01-01", None)])
    for start, end in ranges:
        page, total = 1, 1
        while page <= min(total, 500):   # lapozás, de max. 500 oldal
            p = dict(
                watch_region=region,                    # melyik ország kínálata
                with_watch_providers=NETFLIX_ID,        # csak Netflix
                with_watch_monetization_types="flatrate",  # csak előfizetésben (nem kölcsönzés)
                language="de-DE",                       # német címek
                sort_by="popularity.desc",
                page=page)
            if start: p["primary_release_date.gte"] = start   # gte = "nagyobb vagy egyenlő"
            if end:   p["primary_release_date.lte"] = end     # lte = "kisebb vagy egyenlő"
            d = tmdb("/discover/movie", **p)
            total = d.get("total_pages", 0)
            for m in d.get("results", []):
                found[str(m["id"])] = m
            page += 1
            time.sleep(0.05)   # udvariassági szünet, nehogy kitiltson a szerver
    return found


def omdb(imdb_id):
    """Egy film értékelései az OMDb-től, IMDb-azonosító alapján.
    Visszaad: {"imdb": 8.8, "rt": 81, "mc": 67, "votes": 2500000}"""
    try:
        d = get_json(f"https://www.omdbapi.com/?i={imdb_id}&apikey={OMDB_KEY}", retries=1)
    except urllib.error.HTTPError as e:
        # 401 = a napi keret elfogyott (vagy rossz a kulcs). Nincs értelme újrapróbálni.
        if e.code == 401:
            raise RuntimeError("OMDb: napi limit elérve vagy hibás kulcs – holnap folytatódik")
        raise
    if d.get("Response") == "False":            # az OMDb szövegesen jelez hibát
        if "limit" in d.get("Error", "").lower():
            raise RuntimeError("OMDb: napi limit elérve")
        return {}                               # nincs ilyen film -> üres eredmény
    r = {"imdb": None, "rt": None, "mc": None, "votes": None}
    # try/except: ha az adat hiányzik vagy nem szám, maradjon None (üres)
    try: r["imdb"] = float(d.get("imdbRating"))          # "8.8" -> 8.8
    except (TypeError, ValueError): pass
    try: r["votes"] = int(d.get("imdbVotes", "").replace(",", ""))  # "2,500,000" -> 2500000
    except ValueError: pass
    for s in d.get("Ratings", []):
        if s["Source"] == "Rotten Tomatoes":
            r["rt"] = int(s["Value"].rstrip("%"))        # "81%" -> 81
        elif s["Source"] == "Metacritic":
            r["mc"] = int(s["Value"].split("/")[0])      # "67/100" -> 67
    return r


# --- főprogram --------------------------------------------------------------

def main():
    os.makedirs("data", exist_ok=True)     # a data mappa létrehozása, ha nincs
    db = {"movies": {}}                    # üres alap, ha még nincs fájl
    if os.path.exists(DB_FILE):            # ha van, betöltjük a tegnapi állapotot
        with open(DB_FILE, encoding="utf-8") as f:
            db = json.load(f)
    movies = db["movies"]                  # rövidítés: a filmek szótára

    # === 1. LÉPÉS: mi fut most? ============================================
    for region in REGIONS:
        print(f"Netflix {region} betöltése …")
        current = fetch_region(region)     # a friss lista az internetről
        prev = sum(1 for m in movies.values() if m["avail"].get(region, {}).get("on"))
        print(f"  {len(current)} film (korábban elérhető: {prev})")

        # Biztonsági fék: ha a friss lista gyanúsan kicsi (pl. API-hiba),
        # NEM jelölünk semmit eltűntnek, inkább kihagyjuk ezt a lépést.
        safe = len(current) >= 0.5 * prev
        if not safe:
            print("  FIGYELEM: túl kevés találat, a kivezetés kimarad.")

        # a) ami a friss listán van: felvesszük vagy frissítjük
        for mid, m in current.items():
            e = movies.setdefault(mid, {           # setdefault: ha nincs, létrehozza
                "id": int(mid),
                "title": m.get("title"),           # német cím
                "orig": m.get("original_title"),   # eredeti cím
                "year": (m.get("release_date") or "")[:4] or None,  # "1999-10-15" -> "1999"
                "poster": m.get("poster_path"),    # csak az útvonal, a képet a böngésző tölti
                "added": TODAY,                    # mikor került a katalógusba
                "avail": {},                       # országonkénti elérhetőség
                "r": None,                         # értékelések, egyelőre üres
            })
            e["title"] = m.get("title") or e["title"]
            e["poster"] = m.get("poster_path") or e.get("poster")
            e["genres"] = m.get("genre_ids") or e.get("genres") or []   # műfaj-azonosítók
            e["lang"] = m.get("original_language") or e.get("lang")     # eredeti nyelv, pl. "hi"
            a = e["avail"].setdefault(region, {"first": TODAY})  # first = mióta van fent
            a.update(on=True, last=TODAY)          # on = most elérhető, last = utolsó észlelés

        # b) ami eltűnt a listáról: nem töröljük, csak megjelöljük
        if safe:
            for mid, e in movies.items():
                a = e["avail"].get(region)
                if a and a.get("on") and mid not in current:
                    a["on"] = False
                    a["gone"] = TODAY              # gone = ekkor tűnt el

    # === 2. LÉPÉS: részletek az új filmekhez ===============================
    # Egy hívásból négy adat: magyar cím, játékidő, gyártó ország, IMDb-azonosító.
    # Csak azoknál kérdezünk, ahol valamelyik még hiányzik -> gyors napi futás.
    missing = [e for e in movies.values()
               if "imdb_id" not in e or "title_hu" not in e
               or "runtime" not in e or "country" not in e]
    print(f"Részletek lekérése {len(missing)} filmhez …")
    for e in missing:
        try:
            # append_to_response: két lekérés egyben (adatlap + külső azonosítók)
            d = tmdb(f"/movie/{e['id']}", language="hu-HU", append_to_response="external_ids")
            e["title_hu"] = d.get("title") or e.get("orig")
            e["runtime"] = d.get("runtime") or None           # játékidő percben
            pc = d.get("production_countries") or []
            # fő ország: elsőként az origin_country, ha nincs, az első gyártó ország
            e["country"] = (d.get("origin_country") or [None])[0] or (pc[0]["iso_3166_1"] if pc else None)
            e["imdb_id"] = (d.get("external_ids") or {}).get("imdb_id") or d.get("imdb_id")
        except Exception as ex:
            print(f"  {e['id']}: {ex}")   # egy hibás film nem állítja meg a futást
        time.sleep(0.03)

    # === 3. LÉPÉS: értékelések (napi keret) ================================
    if OMDB_KEY:
        cutoff = (datetime.now() - timedelta(days=RATING_MAX_AGE_DAYS)).date().isoformat()
        is_on = lambda e: any(a.get("on") for a in e["avail"].values())  # bárhol elérhető?
        # Sorrend: 1) akinek még egyáltalán nincs értékelése
        todo = [e for e in movies.values() if e.get("imdb_id") and not e.get("r_date")]
        # 2) akinek régi (45 napnál idősebb), és a film most is elérhető
        todo += sorted([e for e in movies.values()
                        if e.get("imdb_id") and e.get("r_date")
                        and e["r_date"] < cutoff and is_on(e)],
                       key=lambda e: e["r_date"])   # a legrégebbi előre
        print(f"Hiányzó értékelés: {len(todo)}, ma legfeljebb {OMDB_LIMIT}")
        for e in todo[:OMDB_LIMIT]:        # [:950] = csak az első 950 elem
            try:
                e["r"] = omdb(e["imdb_id"])
                e["r_date"] = TODAY        # mikor kértük le -> később ez alapján frissül
            except RuntimeError as ex:
                print(f"  {ex}"); break    # limit elérve: kilépünk a ciklusból
            except Exception as ex:
                print(f"  {e['imdb_id']}: {ex}")
            time.sleep(0.05)

    # === 4. LÉPÉS: mentés ==================================================
    db["updated"] = datetime.now().isoformat(timespec="minutes")  # "utoljára frissítve"
    db["regions"] = REGIONS

    # Műfajnevek németül és magyarul, hogy a weboldal ki tudja írni őket
    try:
        names = {}
        for lang, code in (("de", "de-DE"), ("hu", "hu-HU")):
            for g in tmdb("/genre/movie/list", language=code).get("genres", []):
                names.setdefault(str(g["id"]), {})[lang] = g["name"]
        db["genres"] = names
    except Exception as ex:
        print(f"Műfajok: {ex}")

    with open(DB_FILE, "w", encoding="utf-8") as f:
        # ensure_ascii=False: az ékezetek maradjanak olvashatók
        # separators: felesleges szóközök nélkül -> kisebb fájl
        json.dump(db, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Kész: {len(movies)} film az adatbázisban.")


# Ez a sor csak akkor indítja a main()-t, ha a fájlt közvetlenül futtatjuk.
if __name__ == "__main__":
    sys.exit(main())
