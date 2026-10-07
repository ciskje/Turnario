"""Real-browser test of Turnario via Playwright (dev-only, never deployed).

Own throwaway DB under the opencode temp dir: neither the local test DB
(turnario.db) nor production is touched. Run: python test_browser.py
"""
import os
import threading
from http.server import ThreadingHTTPServer

PID = os.getpid()
DB = os.path.join(os.environ.get("TEMP", "."), f"turnario_bw_{PID}.db")
PORT = 8801 + PID % 300
BASE = f"http://127.0.0.1:{PORT}"

import app as turnario


def main():
    c = turnario.db(DB)
    turnario.create_user(c, "boss", "0000", admin=True)
    elena = turnario.create_user(c, "Elena", "1234")
    marco = turnario.create_user(c, "Marco", "5678")
    g = turnario.create_group(c, "Amici")
    turnario.add_member(c, g["id"], elena)
    turnario.add_member(c, g["id"], marco)

    turnario.Handler.conn = c
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), turnario.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    from playwright.sync_api import sync_playwright
    errors = []
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page()
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(BASE + "/", wait_until="networkidle")

        assert "Entrare" in pg.content(), "login gate renders"
        pg.get_by_placeholder("nome").fill("Elena")
        pg.get_by_placeholder("PIN").fill("1234")
        pg.get_by_role("button", name="Entrare").click()
        pg.get_by_text("Ciao Elena").wait_for(timeout=8000)
        pg.get_by_role("link", name="Apri").click()
        pg.get_by_role("button", name="A chi tocca?").wait_for(timeout=8000)
        assert f"/g/{g['code']}/coffee" in pg.url, "single group redirects to ledger"
        print("ok: login + ledger")

        pg.click(".nav a[href$='/car']")
        pg.get_by_role("button", name="A chi tocca?").wait_for(timeout=8000)
        assert "/car" in pg.url, "macchina ledger opens"
        print("ok: nav macchina")

        pg.click(".nav a[href$='/members']")
        pg.get_by_text("Membri").first.wait_for(timeout=8000)
        body = pg.content()
        assert "Elena" in body and "Marco" in body, "members listed"
        print("ok: nav membri")

        pg.click(".nav a[href$='/coffee']")
        pg.get_by_role("button", name="A chi tocca?").click()
        pg.locator(".result .name").wait_for(timeout=8000)
        payer = pg.locator(".result .name").inner_text()
        assert payer in ("Elena", "Marco"), f"payer shown: {payer}"
        print(f"ok: draw pays {payer}")

        pg.get_by_role("button", name="elimina e rifai").click()
        pg.get_by_role("button", name="A chi tocca?").wait_for(timeout=8000)
        print("ok: delete event restores the form")

        pg.click(".nav a[href='/logout']")
        pg.get_by_role("button", name="Entrare").wait_for(timeout=8000)
        print("ok: logout")

        pg.get_by_placeholder("nome").fill("boss")
        pg.get_by_placeholder("PIN").fill("0000")
        pg.get_by_role("button", name="Entrare").click()
        pg.get_by_text("Ciao boss").wait_for(timeout=8000)
        pg.click("a.btn[href='/admin']")
        pg.get_by_text("Nuovo gruppo").wait_for(timeout=8000)
        pg.get_by_placeholder("nome gruppo").fill("Prova")
        pg.get_by_role("button", name="Crea").first.click()
        pg.get_by_text("creato").wait_for(timeout=8000)
        assert "Prova" in pg.content(), "group created"
        print("ok: admin creates group")

        form = pg.locator("section", has_text="Nuovo utente")
        form.get_by_placeholder("nome", exact=True).fill("Nuovo")
        form.get_by_placeholder("PIN 4 cifre").fill("4321")
        form.get_by_role("button", name="Crea").click()
        pg.get_by_text("utente creato").wait_for(timeout=8000)
        assert "Nuovo" in pg.content(), "user created"
        print("ok: admin creates user")
        b.close()

    srv.shutdown()
    assert not errors, f"console errors: {errors}"
    print("browser test pass: login + nav + draw + admin, zero console errors")


if __name__ == "__main__":
    main()
