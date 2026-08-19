"""Browser smoke test for the web UI.

NOT collected by pytest — it needs a real browser and a running server, and a suite that
cannot run offline in two seconds stops being run. This is the thing you execute by hand
after touching `static/index.html`:

    python tests/ui/fixture_server.py &        # a throwaway vault + review queue on :8099
    python tests/ui/smoke.py                   # screenshots land in /tmp/uitest/shots

Why it exists: the UI was the one part of this project with no test at all, and it showed.
Clicking "Run doctor" returned a 500, printed a traceback into a console nobody was watching,
and changed nothing on screen — a failure no unit test would have caught, because every
individual piece worked. What was broken was the wiring, and the silence.

So this drives the real thing: every view renders, a job runs end to end, an approval leaves
the queue, and — the assertion that matters most — **a failed request raises a visible
toast**. It also fails on any console error, because a page logging errors to nobody is
exactly where this started.
"""

import json, pathlib, sys
from playwright.sync_api import sync_playwright

OUT = pathlib.Path("/tmp/uitest/shots"); OUT.mkdir(exist_ok=True)
errors, failures = [], []

with sync_playwright() as pw:
    b = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium" if pathlib.Path("/opt/pw-browsers/chromium").exists() else None)
    page = b.new_page(viewport={"width": 1380, "height": 900})
    def on_console(m):
        if m.type != "error":
            return
        text = m.text
        if "favicon" in text or "422" in text:   # the 422 is the deliberate bad-kind probe
            return
        errors.append(f"{m.type}: {text}")
    page.on("console", on_console)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    seen404 = []
    page.on("response", lambda r: seen404.append(f"{r.status} {r.url}") if r.status >= 400 else None)
    page.goto("http://127.0.0.1:8099/", wait_until="networkidle")

    def check(name, cond, detail=""):
        (failures if not cond else []).append(f"{name}: {detail}") if not cond else None
        print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))

    # --- chat / shell
    check("title renders", page.inner_text("#viewTitle") == "Chat")
    check("token substituted", "__YOYO_TOKEN__" not in page.content())
    check("health pill resolved", "checking…" not in page.inner_text("#health"),
          page.inner_text("#health"))
    check("memory badge shows pending", page.inner_text("#memBadge").strip() == "3",
          page.inner_text("#memBadge"))
    page.screenshot(path=str(OUT / "1-chat.png"))

    # --- memory
    page.click('[data-view="memory"]')
    page.wait_for_timeout(600)
    check("memory tiles rendered", "waiting on you" in page.inner_text("#memTiles").lower(),
          page.inner_text("#memTiles")[:80])
    check("subjects grouped", page.locator(".subject").count() == 2,
          str(page.locator(".subject").count()))
    check("quote shown with claim", "my sister Priya" in page.inner_text("#proposals"))
    page.screenshot(path=str(OUT / "2-memory.png"))

    # approve one claim, check it disappears
    before = page.locator(".claim").count()
    page.locator('.claim button[data-status="approved"]').first.click()
    page.wait_for_timeout(700)
    check("approving removes it from the queue", page.locator(".claim").count() == before - 1,
          f"{before} -> {page.locator('.claim').count()}")

    # --- map
    page.click('[data-view="map"]')
    page.wait_for_timeout(900)
    check("map drew nodes", page.locator("#map circle").count() >= 3,
          str(page.locator("#map circle").count()))
    check("map counts yoyo pages", "yoyo's pages" in page.inner_text("#mapTiles").lower(),
          page.inner_text("#mapTiles")[:80])
    page.screenshot(path=str(OUT / "3-map.png"))

    # --- health: run doctor (it will FAIL checks offline — that is the interesting case)
    page.click('[data-view="health"]')
    page.wait_for_timeout(400)
    check("jobs empty state", "No jobs yet" in page.inner_text("#jobList"),
          page.inner_text("#jobList")[:80])
    page.click('[data-job=\'{"kind":"doctor"}\']')
    page.wait_for_timeout(4000)
    log = page.inner_text("#jobLog")
    check("doctor job produced a log", len(log.strip()) > 0, log[:200])
    check("toast confirmed the start", page.locator(".toast").count() >= 1)
    check("doctor table rendered", "PASS" in page.inner_text("#doctorTable")
                                   or "FAIL" in page.inner_text("#doctorTable"),
          page.inner_text("#doctorTable")[:120])
    page.screenshot(path=str(OUT / "4-health.png"))

    # --- error surfacing: a job kind that does not exist must toast, not go silent
    page.evaluate("""() => window.__t = document.querySelectorAll('.toast').length""")
    page.evaluate("""async () => { await runJob('nope-not-a-kind', {}); }""")
    page.wait_for_timeout(800)
    grew = page.evaluate("() => document.querySelectorAll('.toast').length > window.__t")
    check("a bad request raises a toast", grew)
    page.screenshot(path=str(OUT / "5-error-toast.png"))

    # --- dark mode (the default this UI is designed for)
    page.emulate_media(color_scheme="dark")
    page.click('[data-view="chat"]')
    page.wait_for_timeout(300)
    check("empty chat offers examples", page.locator(".example").count() == 3)
    page.locator(".example").first.click()
    check("an example fills the box", len(page.input_value("#q")) > 10, page.input_value("#q"))
    page.screenshot(path=str(OUT / "0-dark-chat.png"))
    page.click('[data-view="memory"]'); page.wait_for_timeout(500)
    page.screenshot(path=str(OUT / "0-dark-memory.png"))
    check("composer hidden off the chat tab", page.locator("#ask").is_hidden())

    # --- light mode render
    page.emulate_media(color_scheme="light")
    page.click('[data-view="chat"]')
    page.wait_for_timeout(300)
    page.screenshot(path=str(OUT / "6-light.png"))

    # --- narrow viewport
    page.set_viewport_size({"width": 430, "height": 860})
    page.wait_for_timeout(300)
    page.screenshot(path=str(OUT / "7-narrow.png"))

    b.close()

print("\nHTTP >=400:", seen404)
print("\nconsole errors:", json.dumps(errors, indent=2) if errors else "none")
print("failures:", failures or "none")
sys.exit(1 if (failures or errors) else 0)
