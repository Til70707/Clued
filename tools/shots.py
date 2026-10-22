"""Open the app like an iPhone with REAL data (Wikipedia, maps, World Bank) and save screenshots."""
import http.server, threading, functools, json, os, time, pathlib, traceback
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "shots"; OUT.mkdir(exist_ok=True)
for f in OUT.glob("*"): f.unlink()
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8080), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT)))
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
        info = pg.evaluate("(()=>{const s=document.querySelector('.slide.playing'); if(!s) return null; const c=S.cards[+s.dataset.i]; return c?{kind:c.kind, vis:c.vis||null, title:(c.title||c.q||'').slice(0,80)}:null})()")
        wait = 7500 if info and info.get("vis") == "globe" else 5000 if info and info.get("vis") == "chart" else 2500
        pg.wait_for_timeout(wait)
        name = f"{engine}-{lang}-{i:02d}-{(info or {}).get('kind','x')}-{(info or {}).get('vis') or 'plain'}.png"
        pg.screenshot(path=str(OUT / name))
        log.append(f"[{engine}/{lang}] card {i}: {json.dumps(info, ensure_ascii=False)}")
        pg.evaluate("document.getElementById('feed').scrollBy(0, innerHeight)")
        pg.wait_for_timeout(900)
    for tab in ("news",):
        t = pg.query_selector(f"[data-feed={tab}]")
        if t: t.click(); pg.wait_for_timeout(2500); pg.screenshot(path=str(OUT / f"{engine}-{lang}-tab-{tab}.png"))
    ctx.close(); b.close()

with sync_playwright() as p:
    for engine in ("webkit",):
        for lang in LANGS:
            try: run(p, engine, lang)
            except Exception: log.append(f"[{engine}/{lang}] CRASH {traceback.format_exc()}")
(OUT / "log.txt").write_text("\n".join(log), encoding="utf-8")
srv.shutdown()
print("\n".join(log[-60:]))
