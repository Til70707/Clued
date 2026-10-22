"""Clued daily content builder.

For one language it:
  1. reads today's world news from Wikipedia (falls back to English news when that Wikipedia has none),
  2. reads the curated geography / economics topics (the lists live in index.html, one source of truth),
  3. attaches real shapes from Natural Earth (lakes, rivers, regions, seas) so the globe can colour them,
  4. narrates every card with a neural voice (Kokoro or Piper) into small MP3 files.

Output: out/content/<lang>/{news.json, topics.json, a/*.mp3}
Usage:  python tools/build_content.py <lang>
"""
import hashlib, html, json, os, re, subprocess, sys, time, urllib.parse, urllib.request, wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

LANG = sys.argv[1] if len(sys.argv) > 1 else "en"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "content" / LANG
CACHE = ROOT / ".cache-content" / LANG          # persisted between runs (actions/cache)
AUDIO_CACHE = CACHE / "audio"
for d in (OUT / "a", AUDIO_CACHE): d.mkdir(parents=True, exist_ok=True)
LOG = []
def log(*a):
    s = " ".join(str(x) for x in a); print(s, flush=True); LOG.append(s)

UA = {"User-Agent": "CluedBot/1.0 (https://github.com/Til70707/clued; educational app)", "Accept": "application/json"}
def get(url, tries=3, raw=False):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
                data = r.read()
                return data if raw else json.loads(data)
        except urllib.error.HTTPError as e:
            if e.code == 404: return None
            time.sleep(1.5 * (k + 1))
        except Exception:
            time.sleep(1.5 * (k + 1))
    return None

# ---------- shared rules (same as the app) ----------
BLOCK = re.compile(r"porn|onlyfans|hentai|\bsex(ual|y)?\b|\bnud(e|ity|ism|ist)\b|erotic|fetish|\bbdsm\b|\bescort\b|\bstripper|\brape\b|incest|camgirl", re.I)
MINOR = re.compile(r"nogomet|košark|fußball|fussball|fútbol|calcio|baloncesto|pallacanestro|schauspieler|attore|(^|\s)acteur|igral(ec|ka)|pev(ec|ka)|sänger|cantante|chanteu|football|soccer|basketball|tennis|cricket|\bracing|cyclist|\bgolf\b|rugby|baseball|ice hockey|athlete|footballer|\bsports?\b|tournament|championship|formula one|grand prix|olympic|\bfilm\b|album|singer|\bactor|\bactress|rapper|\bband\b|television series|video game|beauty pageant|music festival|award ceremony", re.I)
SKIP_TITLES = re.compile(r"^(Main_Page|Special:|Wikipedia:|File:|Portal:|Help:|Template:|Category:)")
# pictures that make bad backgrounds: logos, flags, emblems, slogans, maps
BAD_IMG = re.compile(r"\.svg($|/)|flag|logo|emblem|coat_of_arms|seal_of|wappen|grb_|banner|slogan|locator|location_map|_map\b|map_of", re.I)

def strip_html(h):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", h or ""))).strip()

def trim(t, mx=420):
    t = re.sub(r"\s+", " ", t or "").strip()
    if len(t) <= mx: return t
    cut = t[:mx]; dot = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[:dot + 1] if dot > mx * 0.45 else re.sub(r"\s+\S*$", "", cut) + "…"

def for_speech(t):
    t = re.sub(r"\s*\([^()]*\)", "", t)            # drop "(pronunciation; other names)"
    t = re.sub(r"\s*\[[^\]]*\]", "", t)
    t = t.replace("…", ".").replace("–", ", ").replace("—", ", ")
    return re.sub(r"\s+", " ", t).strip()

def ok_page(p):
    if not p or not p.get("titles"): return False
    t = p["titles"].get("canonical") or p.get("title") or ""
    if SKIP_TITLES.search(t) or BLOCK.search(t) or BLOCK.search(p.get("extract") or "") or BLOCK.search(p.get("description") or ""): return False
    return p.get("type") != "disambiguation" and len(p.get("extract") or "") > 80

def good_image(p):
    o, th = p.get("originalimage") or {}, p.get("thumbnail") or {}
    src = o.get("source") or th.get("source") or ""
    if not src or BAD_IMG.search(src): return None
    if (o.get("width") or 0) <= 1280 and re.search(r"\.(jpe?g|png|webp)$", o.get("source") or "", re.I):
        big = o["source"]
    else:
        t = th.get("source") or ""
        big = re.sub(r"/\d+px-", "/1280px-", t) if re.search(r"/\d+px-", t) else (o.get("source") or t)
    return {"img": big, "thumb": th.get("source") or big, "imgW": o.get("width") or th.get("width") or 0, "imgH": o.get("height") or th.get("height") or 0}

def card_from_page(p, kind, **extra):
    title = (p.get("titles") or {}).get("normalized") or p.get("title")
    c = {"id": f"{kind}:{(p.get('titles') or {}).get('canonical') or title}", "kind": kind, "title": title, "name": title,
         "desc": p.get("description") or "", "text": trim(p.get("extract")), "url": ((p.get("content_urls") or {}).get("mobile") or {}).get("page") or "",
         "img": "", "thumb": "", "imgW": 0, "imgH": 0}
    im = good_image(p)
    if im: c.update(im)
    co = p.get("coordinates")
    if co and "lat" in co and "lon" in co: c["coords"] = [round(co["lon"], 3), round(co["lat"], 3)]
    c.update(extra)
    return c

# ---------- topic lists from index.html ----------
SRC = (ROOT / "index.html").read_text(encoding="utf-8")
def js_value(name):
    m = re.search(r"const " + name + r" = (.+?);\n", SRC)
    return json.loads(m.group(1))
CAPITALS = js_value("CAPITALS"); GEO = js_value("GEO_TOPICS"); ECON = js_value("ECON_TOPICS")
TOPICS = [(t, "geo") for t in GEO] + [(t, "econ") for t in ECON] + [(t, "geo") for t in CAPITALS]
seen = set(); TOPICS = [x for x in TOPICS if not (x[0] in seen or seen.add(x[0]))]

# ---------- map data ----------
def load_geo(url, name):
    f = ROOT / ".cache-geo" / name
    f.parent.mkdir(exist_ok=True)
    if not f.exists():
        data = get(url, raw=True)
        if data: f.write_bytes(data)
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None

NE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
layers = {
    "lake": load_geo(NE + "ne_10m_lakes.geojson", "lakes10.json"),
    "river": load_geo(NE + "ne_50m_rivers_lake_centerlines.geojson", "rivers50.json"),
    "region": load_geo(NE + "ne_50m_geography_regions_polys.geojson", "regions50.json"),
    "sea": load_geo(NE + "ne_50m_geography_marine_polys.geojson", "marine50.json"),
}
# English topic -> (layer, Natural Earth name). Names checked case-insensitively.
FEATURE = {
    "Lake Baikal": ("lake", "Lake Baikal"), "Caspian Sea": ("lake", "Caspian Sea"), "Aral Sea": ("lake", "Aral Sea"), "Dead Sea": ("lake", "Dead Sea"),
    "Lake Bled": ("lake", "Bled"),
    "Nile": ("river", "Nile"), "Danube": ("river", "Danube"), "Amazon River": ("river", "Amazonas"), "Yangtze": ("river", "Chang Jiang"),
    "Ganges": ("river", "Ganges"), "Mississippi River": ("river", "Mississippi"), "Congo River": ("river", "Congo"), "Mekong": ("river", "Mekong"),
    "Sahara": ("region", "SAHARA"), "Gobi Desert": ("region", "GOBI DESERT"), "Atacama Desert": ("region", "ATACAMA DESERT"), "Namib": ("region", "NAMIB DESERT"),
    "Himalayas": ("region", "HIMALAYAS"), "Alps": ("region", "ALPS"), "Andes": ("region", "ANDES"), "Rocky Mountains": ("region", "ROCKY MOUNTAINS"),
    "Ural Mountains": ("region", "URAL MOUNTAINS"), "Caucasus": ("region", "CAUCASUS MTS."), "Patagonia": ("region", "PATAGONIA"), "Siberia": ("region", "SIBERIA"),
    "Sahel": ("region", "SAHEL"), "Balkans": ("region", "BALKAN PENINSULA"), "Scandinavia": ("region", "SCANDINAVIA"), "Great Rift Valley": ("region", "GREAT RIFT VALLEY"),
    "Amazon rainforest": ("region", "AMAZON BASIN"), "Horn of Africa": ("region", "HORN OF AFRICA"), "Central Asia": ("region", "CENTRAL ASIA"),
    "Mediterranean Sea": ("sea", "Mediterranean Sea"), "Adriatic Sea": ("sea", "Adriatic Sea"), "South China Sea": ("sea", "South China Sea"),
    "Pacific Ocean": ("sea", "North Pacific Ocean|South Pacific Ocean"), "Atlantic Ocean": ("sea", "North Atlantic Ocean|South Atlantic Ocean"),
    "Indian Ocean": ("sea", "Indian Ocean"), "Arctic Ocean": ("sea", "Arctic Ocean"), "Strait of Hormuz": ("sea", "Strait of Hormuz|Persian Gulf"),
    "Strait of Malacca": ("sea", "Strait of Malacca|Malacca Strait"), "Strait of Gibraltar": ("sea", "Strait of Gibraltar"), "Bosporus": ("sea", "Bosporus|Bosphorus"),
    "Great Barrier Reef": ("region", "GREAT BARRIER REEF"),
}
def round_geom(g, nd=2):
    def r(c):
        if isinstance(c[0], (int, float)): return [round(c[0], nd), round(c[1], nd)]
        return [r(x) for x in c]
    return {"type": g["type"], "coordinates": r(g["coordinates"])}
try:
    from shapely.geometry import shape, mapping
    from shapely.ops import unary_union
except Exception:
    shape = None
def find_feature(topic):
    spec = FEATURE.get(topic)
    if not spec or not shape: return None
    layer, names = spec
    fc = layers.get(layer)
    if not fc: return None
    want = [n.lower() for n in names.split("|")]
    hits = [f for f in fc["features"] if any(((f["properties"].get(k) or "").lower() == w) for w in want for k in ("name", "NAME", "name_en", "NAME_EN"))]
    if not hits:
        hits = [f for f in fc["features"] if any(w in (f["properties"].get("name") or f["properties"].get("NAME") or "").lower() for w in want)]
    if not hits: log("  no shape for", topic); return None
    geom = unary_union([shape(f["geometry"]) for f in hits])
    minx, miny, maxx, maxy = geom.bounds
    tol = max(0.01, min(0.3, max(maxx - minx, maxy - miny) / 250))
    geom = geom.simplify(tol, preserve_topology=True)
    return {"ftype": layer, "geom": round_geom(mapping(geom)), "bbox": [round(minx, 2), round(miny, 2), round(maxx, 2), round(maxy, 2)]}

# country names as used by the world map (for spotting where a news story happened)
WA = get("https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/countries-110m.json") or {}
COUNTRIES = {g["properties"]["name"] for g in (WA.get("objects", {}).get("countries", {}).get("geometries", []))}
COUNTRY_ALIAS = {"United States": "United States of America", "Czech Republic": "Czechia", "Bosnia and Herzegovina": "Bosnia and Herz.", "North Macedonia": "Macedonia", "Democratic Republic of the Congo": "Dem. Rep. Congo"}

# ---------- Wikipedia ----------
API = f"https://{LANG}.wikipedia.org/api/rest_v1"
def summary(lang, title):
    return get(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title.replace(' ', '_'), safe='')}")

def lang_titles(titles):
    """English title -> title in LANG (via interlanguage links)."""
    if LANG == "en": return {t: t for t in titles}
    out = {}
    for i in range(0, len(titles), 50):
        chunk = titles[i:i + 50]
        q = get("https://en.wikipedia.org/w/api.php?action=query&format=json&formatversion=2&redirects=1&prop=langlinks&lllang=" + LANG
                + "&lllimit=max&titles=" + urllib.parse.quote("|".join(chunk)))
        if not q: continue
        qq = q.get("query", {}); alias = {a["from"]: a["to"] for a in qq.get("normalized", []) + qq.get("redirects", [])}
        ll = {p["title"]: (p.get("langlinks") or [{}])[0].get("title") for p in qq.get("pages", [])}
        for t in chunk:
            k = t
            for _ in range(3):
                if k in alias: k = alias[k]
            if ll.get(k): out[t] = ll[k]
    return out

def news_cards(feed, lang_tag):
    cards = []
    for n in (feed or {}).get("news", []):
        story_html = n.get("story") or ""; story = strip_html(story_html)
        links = n.get("links") or []
        pages = [p for p in links if ok_page(p)]
        if not story or not pages or BLOCK.search(story): continue
        if any(MINOR.search(p.get("description") or "") for p in links): continue
        # the bold link in the story is the event itself; its picture fits the story
        bold = re.search(r"<b>\s*<a [^>]*href=\"\./([^\"#]+)", story_html)
        event = None
        if bold:
            bt = urllib.parse.unquote(bold.group(1)).replace("_", " ")
            event = next((p for p in pages if ((p.get("titles") or {}).get("normalized") or p.get("title", "").replace("_", " ")) == bt
                          or ((p.get("titles") or {}).get("canonical") or "").replace("_", " ") == bt), None)
        main = event or sorted(pages[:4], key=lambda p: -len(p.get("extract") or ""))[0]
        c = card_from_page(main, "news")
        c["id"] = "news:" + hashlib.sha1(story[:120].encode()).hexdigest()[:12]
        c["title"] = trim(story, 140); c["headline"] = story
        c["text"] = trim(story + " " + (main.get("extract") or ""))
        c["topic"] = (main.get("titles") or {}).get("normalized") or main.get("title")
        c["links"] = [(p.get("titles") or {}).get("normalized") or p.get("title") for p in pages]
        if event is None or not good_image(event):  # no picture of the event itself: show where it happened instead
            c.update({"img": "", "thumb": "", "imgW": 0, "imgH": 0})
        country = next((COUNTRY_ALIAS.get(t, t) for t in c["links"] if COUNTRY_ALIAS.get(t, t) in COUNTRIES), None)
        if country: c["country"] = country
        if lang_tag != LANG: c["lang"] = lang_tag
        c["say"] = for_speech(c["text"])
        cards.append(c)
    return cards

def fetch_feed(lang, day):
    return get(f"https://{lang}.wikipedia.org/api/rest_v1/feed/featured/{day:%Y/%m/%d}")

# ---------- voices ----------
KOKORO = {"en": ("a", "am_michael"), "es": ("e", "em_alex"), "it": ("i", "im_nicola")}
PIPER = {"de": "de/de_DE/thorsten/high/de_DE-thorsten-high", "fr": "fr/fr_FR/tom/medium/fr_FR-tom-medium", "sl": "sl/sl_SI/artur/medium/sl_SI-artur-medium"}
_engine = {}
def synth_wav(text, wav_path):
    if LANG in KOKORO:
        if "k" not in _engine:
            from kokoro import KPipeline
            _engine["k"] = KPipeline(lang_code=KOKORO[LANG][0])
        import numpy as np, soundfile as sf
        parts = [a for _, _, a in _engine["k"](text, voice=KOKORO[LANG][1], speed=0.95)]
        audio = np.concatenate([p.numpy() if hasattr(p, "numpy") else p for p in parts])
        sf.write(str(wav_path), audio, 24000)
    else:
        if "p" not in _engine:
            base = "https://huggingface.co/rhasspy/piper-voices/resolve/main/" + PIPER[LANG]
            mdir = ROOT / ".cache-geo" / "piper"; mdir.mkdir(parents=True, exist_ok=True)
            onnx = mdir / (Path(PIPER[LANG]).name + ".onnx")
            for suf in ("", ".json"):
                f = Path(str(onnx) + suf)
                if not f.exists():
                    data = get(base + ".onnx" + suf, raw=True)
                    if not data: raise RuntimeError("piper voice download failed")
                    f.write_bytes(data)
            from piper import PiperVoice
            _engine["p"] = PiperVoice.load(str(onnx))
        v = _engine["p"]
        with wave.open(str(wav_path), "wb") as wf:
            if hasattr(v, "synthesize_wav"):
                try:
                    from piper import SynthesisConfig
                    v.synthesize_wav(text, wf, syn_config=SynthesisConfig(length_scale=1.08))
                except Exception:
                    v.synthesize_wav(text, wf)
            else:
                v.synthesize(text, wf, length_scale=1.08)

def voice_id():
    return f"kokoro-{KOKORO[LANG][1]}" if LANG in KOKORO else f"piper-{Path(PIPER[LANG]).name}"

BUDGET = int(os.environ.get("AUDIO_BUDGET", "600")); made = 0; voice_ok = True
def narrate(text):
    """Return the mp3 file name for this text (cached), or '' if no audio could be made."""
    global made, voice_ok
    if not text or not voice_ok: return ""
    h = hashlib.sha1((voice_id() + "|" + text).encode()).hexdigest()[:16] + ".mp3"
    cached = AUDIO_CACHE / h
    if not cached.exists():
        if made >= BUDGET: return ""
        wav = AUDIO_CACHE / (h + ".wav")
        try:
            synth_wav(text, wav)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-ac", "1", "-ar", "24000", "-codec:a", "libmp3lame", "-b:a", "40k", str(cached)], check=True)
            made += 1
        except Exception as e:
            log("  VOICE ERROR:", repr(e)[:300]); voice_ok = made > 0  # stop trying if the engine never worked
            return ""
        finally:
            if wav.exists(): wav.unlink()
    (OUT / "a" / h).write_bytes(cached.read_bytes())
    return h

# ---------- build ----------
def main():
    t0 = time.time()
    now = datetime.now(timezone.utc)
    # news: today, else yesterday; this language first, English if it has none
    news = []
    for lang in ([LANG, "en"] if LANG != "en" else ["en"]):
        for back in range(0, 3):
            feed = fetch_feed(lang, now - timedelta(days=back))
            got = news_cards(feed, lang)
            if got: news = got; break
        if news: break
    # second day of news, so the tab is not empty early in the morning
    if news:
        feed2 = fetch_feed(news[0].get("lang", LANG), now - timedelta(days=1 + back))
        seen_ids = {c["id"] for c in news}
        news += [c for c in news_cards(feed2, news[0].get("lang", LANG)) if c["id"] not in seen_ids][:6]
    for c in news:
        if c.get("country") and not c.get("coords"):
            s = summary("en", next((k for k, v in COUNTRY_ALIAS.items() if v == c["country"]), c["country"]))
            if s and s.get("coordinates"): c["coords"] = [round(s["coordinates"]["lon"], 3), round(s["coordinates"]["lat"], 3)]
    log(f"{LANG}: {len(news)} news cards")

    # picture of the day
    photo = None
    feed = fetch_feed(LANG, now) or fetch_feed("en", now)
    im = (feed or {}).get("image") or {}
    if im.get("image", {}).get("source"):
        d = strip_html((im.get("description") or {}).get("html") or (im.get("description") or {}).get("text") or "")
        if d and not BLOCK.search(d):
            src = im["image"]["source"]; th = (im.get("thumbnail") or {}).get("source") or src
            big = re.sub(r"/\d+px-", "/1280px-", th) if re.search(r"/\d+px-", th) else src
            photo = {"id": "photo:" + im.get("title", ""), "kind": "photo", "title": trim(d, 120), "text": trim(d), "img": big, "thumb": th,
                     "imgW": im["image"].get("width", 0), "imgH": im["image"].get("height", 0), "url": im.get("file_page") or "",
                     "credit": strip_html((im.get("artist") or {}).get("html") or (im.get("artist") or {}).get("text") or ""), "say": for_speech(trim(d))}

    # topics (kept between runs; refreshed weekly)
    prev_file = CACHE / "topics.json"
    prev = {c["en"]: c for c in json.loads(prev_file.read_text(encoding="utf-8"))} if prev_file.exists() else {}
    titles = lang_titles([t for t, _ in TOPICS])
    topics = []
    for t, kind in TOPICS:
        lt = titles.get(t)
        if not lt: continue
        old = prev.get(t)
        if old and old.get("_at", 0) > time.time() - 7 * 86400:
            topics.append(old); continue
        p = summary(LANG, lt)
        if not ok_page(p): continue
        c = card_from_page(p, kind, en=t)
        f = find_feature(t)
        if f: c.update(f)
        c["say"] = for_speech(f"{c['title']}. {c['text']}")
        c["_at"] = time.time()
        topics.append(c)
        time.sleep(0.05)
    prev_file.write_text(json.dumps(topics, ensure_ascii=False), encoding="utf-8")
    log(f"{LANG}: {len(topics)} topics, {sum(1 for c in topics if c.get('geom'))} with shapes")

    # narration: news first (fresh every day), then topics
    for c in news + ([photo] if photo else []) + topics:
        a = narrate(c.get("say", ""))
        if a: c["audio"] = f"content/{LANG}/a/{a}"
    log(f"{LANG}: voice {voice_id()}, {made} new clips, "
        f"{sum(1 for c in news + topics if c.get('audio'))}/{len(news) + len(topics)} cards with audio, voice ok: {voice_ok}")

    clean = lambda cs: [{k: v for k, v in c.items() if not k.startswith("_") and k != "say"} for c in cs]
    (OUT / "news.json").write_text(json.dumps({"date": now.strftime("%Y-%m-%d"), "news": clean(news), "photo": clean([photo])[0] if photo else None}, ensure_ascii=False), encoding="utf-8")
    (OUT / "topics.json").write_text(json.dumps({"topics": clean(topics)}, ensure_ascii=False), encoding="utf-8")
    log(f"{LANG}: done in {time.time() - t0:.0f}s")

try:
    main()
finally:
    (ROOT / "out").mkdir(exist_ok=True)
    (ROOT / "out" / f"build-log-{LANG}.txt").write_text("\n".join(LOG), encoding="utf-8")
