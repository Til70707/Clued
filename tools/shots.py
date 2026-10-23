"""Open the app like an iPhone with REAL data (Wikipedia, maps, World Bank) and save screenshots."""
import http.server, threading, functools, json, os, time, pathlib, traceback
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[1]
SITE = ROOT / os.environ.get("SITE_DIR", ".")
OUT = ROOT / "shots"; OUT.mkdir(exist_ok=True)
for f in OUT.glob("*"):
    if f.is_file(): f.unlink()
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8080), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE)))
threading.Thread(target=srv.serve_forever, daemon=True).start()
log = []
LANGS = os.environ.get("SHOT_LANGS", "en,sl").split(",")

def run(p, engine, lang):
    dev = p.devices["iPhone 14 Pro"] if engine == "webkit" else {"viewport": {"width": 393, "height": 852}, "device_scale_factor": 3, "is_mobile": True, "has_touch": True}
    b = getattr(p, engine).launch()
    ctx = b.new_context(**dev)
    ctx.add_init_script(f"try{{localStorage.setItem('ss.lang', JSON.stringify('{lang}'))}}catch(e){{}}")
    pg = ctx.new_page()
    pg.on("console", lambda m: m.type in ("error", "warning") and log.append(f"[{engine}/{lang}] console {m.type}: {m.text}"))
    pg.on("pageerror", lambda e: log.append(f"[{engine}/{lang}] PAGEERROR: {e}"))
    pg.on("requestfailed", lambda r: log.append(f"[{engine}/{lang}] FAILED {r.url[:140]} {r.failure}"))
    pg.goto("http://127.0.0.1:8080/index.html", wait_until="load")
    pg.wait_for_timeout(5000)
    pg.screenshot(path=str(OUT / f"{engine}-{lang}-00-start.png"))
    for sel in ("[data-lang-pick='%s']" % lang, "#goMuted"):
        el = pg.query_selector(sel)
        if el and el.is_visible(): el.click(); pg.wait_for_timeout(800)
    pg.wait_for_timeout(2500)
    for i in range(1, 15):
        info = pg.evaluate("(()=>{const s=document.querySelector('.slide.playing'); if(!s) return null; const c=S.cards[+s.dataset.i]; return c?{kind:c.kind, vis:c.vis||null, ftype:c.ftype||null, audio:!!c.audio, title:(c.title||c.q||'').slice(0,80)}:null})()")
        wait = 14000 if info and info.get("vis") == "globe" else 5000 if info and info.get("vis") == "chart" else 3000
        pg.wait_for_timeout(wait)
        name = f"{engine}-{lang}-{i:02d}-{(info or {}).get('kind','x')}-{(info or {}).get('vis') or 'plain'}.png"
        pg.screenshot(path=str(OUT / name))
        log.append(f"[{engine}/{lang}] card {i}: {json.dumps(info, ensure_ascii=False)}")
        pg.evaluate("document.getElementById('feed').scrollBy(0, innerHeight)")
        pg.wait_for_timeout(900)
    t = pg.query_selector("[data-feed=news]")
    if t:
        t.click(); pg.wait_for_timeout(2500)
        for k in range(1, 9):
            info = pg.evaluate("(()=>{const s=document.querySelector('.slide.playing'); if(!s) return null; const c=S.cards[+s.dataset.i]; return c?{kind:c.kind, vis:c.vis||null, img:!!c.img, audio:!!c.audio, place:c.place||null, country:c.country||null, lang:c.lang||null, title:(c.title||c.q||'').slice(0,80)}:null})()")
            pg.wait_for_timeout(7500 if info and info.get("vis") == "globe" else 3000)
            pg.screenshot(path=str(OUT / f"{engine}-{lang}-news-{k}-{(info or {}).get('vis') or 'plain'}.png"))
            log.append(f"[{engine}/{lang}] news {k}: {json.dumps(info, ensure_ascii=False)}")
            pg.evaluate("document.getElementById('feed').scrollBy(0, innerHeight)"); pg.wait_for_timeout(900)
    # capital postcards: jump to a few countries that dive into their capital and wait for the photo
    try:
        t = pg.query_selector("[data-feed=all]")
        if t: t.click(); pg.wait_for_timeout(1500)
        ids = pg.evaluate("""(()=>{const sd=s=>[...String(s)].reduce((a,ch)=>(a*31+ch.charCodeAt(0))>>>0,7);
          const l=CONTENT.topics.filter(c=>c.capital&&c.capital.img&&sd(c.id)%2===0).slice(0,3); appendCards(l); return l.map(c=>c.id)})()""")
        for k, cid in enumerate(ids):
            pg.evaluate("(id)=>{const i=S.cards.findIndex(c=>c.id===id); const el=document.querySelector(`.slide[data-i='${i}']`); el&&el.scrollIntoView()}", cid)
            pg.wait_for_timeout(17000)
            pg.screenshot(path=str(OUT / f"{engine}-{lang}-postcard-{k + 1}.png"))
            log.append(f"[{engine}/{lang}] postcard {k + 1}: {cid}")
    except Exception:
        log.append(f"[{engine}/{lang}] postcard CRASH {traceback.format_exc()[-300:]}")
    log.append(f"[{engine}/{lang}] content ok: {pg.evaluate('CONTENT.ok')}, topics: {pg.evaluate('CONTENT.topics.length')}, with audio: {pg.evaluate('CONTENT.topics.filter(c=>c.audio).length')}")
    ctx.close(); b.close()

def picker(p):
    b = p.webkit.launch(); ctx = b.new_context(**p.devices["iPhone 14 Pro"]); pg = ctx.new_page()
    pg.goto("http://127.0.0.1:8080/index.html"); pg.wait_for_timeout(2500)
    pg.screenshot(path=str(OUT / "webkit-00-language-picker.png")); ctx.close(); b.close()

with sync_playwright() as p:
    try: picker(p)
    except Exception: log.append("picker CRASH " + traceback.format_exc())
    for engine in ("webkit",):
        for lang in LANGS:
            try: run(p, engine, lang)
            except Exception: log.append(f"[{engine}/{lang}] CRASH {traceback.format_exc()}")
(OUT / "log.txt").write_text("\n".join(log), encoding="utf-8")
srv.shutdown()
print("\n".join(log[-60:]))
