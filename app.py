#!/usr/bin/env python3
"""Turnario — who pays for coffee / who takes the car. Stdlib only.

  python app.py --port 8080 --db turnario.db
  python app.py --admin admin "PIN" --db turnario.db
  python app.py --test

Code is English; only the strings shown to the user are Italian.
"""
import argparse
import datetime
import hashlib
import html
import random
import re
import os
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

VERSION = "1.5"
TYPES = ("coffee", "car")
LABELS = {"coffee": "caffè", "car": "macchina"}
MONTHS = ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
          "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre")
PIN_RE = re.compile(r"^\d{4}$")
MAX_BODY = 65536  # POST bodies are tiny forms; anything bigger is refused
CHANGELOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "CHANGELOG.md")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, pin_salt TEXT NOT NULL,
  pin_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS groups(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS members(
  id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL REFERENCES groups(id),
  user_id INTEGER NOT NULL REFERENCES users(id), active INTEGER NOT NULL DEFAULT 1,
  joined TEXT NOT NULL, UNIQUE(group_id, user_id));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL REFERENCES groups(id),
  date TEXT NOT NULL, type TEXT NOT NULL, member_id INTEGER NOT NULL REFERENCES members(id),
  UNIQUE(group_id, date, type));
CREATE TABLE IF NOT EXISTS presence(
  event_id INTEGER NOT NULL REFERENCES events(id),
  member_id INTEGER NOT NULL REFERENCES members(id),
  PRIMARY KEY(event_id, member_id));
"""


def db(path):
    # ponytail: one shared connection, check_same_thread=False. WAL handles the
    # traffic of a friend group; switch to a connection per request if needed.
    c = sqlite3.connect(path, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(SCHEMA)
    try:  # older databases lack the archived column
        c.execute("ALTER TABLE groups ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
    except sqlite3.Error:
        pass
    c.commit()
    return c


def hash_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 50000).hex()


def num(v):
    """Form fields are always ints; garbage becomes 0 instead of a 500."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def create_user(c, name, pin, admin=False):
    """Unique name: Elena, Elena2, Elena3..."""
    name = (name or "").strip()[:40]
    if not name:
        return None
    n, i = name, 2
    while c.execute("SELECT 1 FROM users WHERE name=?", (n,)).fetchone():
        n = f"{name}{i}"
        i += 1
    salt = secrets.token_hex(8)
    cur = c.execute("INSERT INTO users(name,pin_salt,pin_hash,is_admin) VALUES(?,?,?,?)",
                    (n, salt, hash_pin(pin, salt), int(admin)))
    c.commit()
    return cur.lastrowid


def get_user(c, header):
    """Cookie auth=<id>:<pin_hash>, self-validating: without the PIN you cannot
    produce the hash, so signing it is unnecessary."""
    for part in (header or "").split(";"):
        part = part.strip()
        if part.startswith("auth="):
            uid, _, h = part[5:].partition(":")
            u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if u and secrets.compare_digest(h, u["pin_hash"]):
                return dict(u)
    return None


def login(c, name, pin):
    u = c.execute("SELECT * FROM users WHERE name=?", ((name or "").strip(),)).fetchone()
    if u and secrets.compare_digest(hash_pin(pin or "", u["pin_salt"]), u["pin_hash"]):
        return dict(u)
    return None


def create_group(c, name):
    name = (name or "").strip()[:40]
    if not name:
        return None
    cur = c.execute("INSERT INTO groups(code,name) VALUES(?,?)", (secrets.token_hex(4), name))
    c.commit()
    return dict(c.execute("SELECT * FROM groups WHERE id=?", (cur.lastrowid,)).fetchone())


def get_group(c, code):
    return c.execute("SELECT * FROM groups WHERE code=? AND archived=0", (code,)).fetchone()


def my_groups(c, user):
    return [dict(r) for r in c.execute(
        "SELECT g.code,g.name FROM groups g JOIN members m ON m.group_id=g.id "
        "WHERE m.user_id=? AND m.active=1 AND g.archived=0 ORDER BY g.id", (user["id"],))]


def is_member(c, gid, uid):
    return c.execute("SELECT 1 FROM members WHERE group_id=? AND user_id=? AND active=1",
                     (gid, uid)).fetchone()


def add_member(c, gid, uid):
    c.execute("INSERT OR IGNORE INTO members(group_id,user_id,joined) VALUES(?,?,?)",
              (gid, uid, datetime.date.today().isoformat()))
    c.commit()


def members(c, gid, active_only=True):
    q = ("SELECT m.id,m.active,m.joined,u.name FROM members m JOIN users u ON u.id=m.user_id "
         "WHERE m.group_id=?" + (" AND m.active=1" if active_only else "") + " ORDER BY u.name")
    return [dict(r) for r in c.execute(q, (gid,))]


def stats(c, gid, typ):
    """(present, paid, debt) per member. Debt = times present - times paying."""
    out = {}
    for m in members(c, gid):
        pres = c.execute("SELECT COUNT(*) FROM presence p JOIN events e ON e.id=p.event_id "
                         "WHERE e.group_id=? AND e.type=? AND p.member_id=?", (gid, typ, m["id"])).fetchone()[0]
        pay = c.execute("SELECT COUNT(*) FROM events WHERE group_id=? AND type=? AND member_id=?",
                        (gid, typ, m["id"])).fetchone()[0]
        out[m["id"]] = (pres, pay, pres - pay)
    return out


def pick(pool, debt):
    """Highest debt wins; ties are random."""
    top = max(debt[m] for m in pool)
    return random.choice([m for m in pool if debt[m] == top])


def record(c, gid, typ, date, present):
    """One event per (group, day, type). Returns the event id, or None if it exists."""
    pool = [m["id"] for m in members(c, gid) if m["id"] in present]
    if not pool:
        return None
    payer_id = pick(pool, {m: stats(c, gid, typ)[m][2] for m in pool})
    try:
        cur = c.execute("INSERT INTO events(group_id,date,type,member_id) VALUES(?,?,?,?)",
                        (gid, date, typ, payer_id))
    except sqlite3.IntegrityError:
        return None
    for m in pool:
        c.execute("INSERT INTO presence(event_id,member_id) VALUES(?,?)", (cur.lastrowid, m))
    c.commit()
    return cur.lastrowid


def delete_event(c, gid, eid):
    c.execute("DELETE FROM presence WHERE event_id IN (SELECT id FROM events WHERE group_id=? AND id=?)", (gid, eid))
    c.execute("DELETE FROM events WHERE group_id=? AND id=?", (gid, eid))
    c.commit()


def delete_user(c, uid):
    """Delete a user and every event they took part in."""
    eids = [r[0] for r in c.execute("SELECT DISTINCT event_id FROM presence "
                                    "WHERE member_id IN (SELECT id FROM members WHERE user_id=?)", (uid,))]
    for eid in eids:
        c.execute("DELETE FROM presence WHERE event_id=?", (eid,))
        c.execute("DELETE FROM events WHERE id=?", (eid,))
    c.execute("DELETE FROM members WHERE user_id=?", (uid,))
    c.execute("DELETE FROM users WHERE id=?", (uid,))
    c.commit()


def today_event(c, gid, typ):
    return c.execute("SELECT e.id,e.date,u.name FROM events e JOIN members m ON m.id=e.member_id "
                     "JOIN users u ON u.id=m.user_id WHERE e.group_id=? AND e.type=? AND e.date=?",
                     (gid, typ, datetime.date.today().isoformat())).fetchone()


def events(c, gid, typ):
    out = []
    for e in c.execute("SELECT e.id,e.date,u.name FROM events e JOIN members m ON m.id=e.member_id "
                       "JOIN users u ON u.id=m.user_id WHERE e.group_id=? AND e.type=? "
                       "ORDER BY e.date DESC LIMIT 30", (gid, typ)):
        pres = [r["name"] for r in c.execute(
            "SELECT u.name FROM presence p JOIN members m ON m.id=p.member_id JOIN users u ON u.id=m.user_id "
            "WHERE p.event_id=?", (e["id"],))]
        out.append((e["date"], e["name"], pres, e["id"]))
    return out


def it_date(iso):
    try:
        dt = datetime.date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt.year}"


def valid_date(s):
    try:
        datetime.date.fromisoformat(s)
        return True
    except (TypeError, ValueError):
        return False


def ping(text):
    """Optional Telegram notification. Silent when not configured."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        urlopen(Request(f"https://api.telegram.org/bot{token}/sendMessage",
                        data=urlencode({"chat_id": chat, "text": text}).encode()), timeout=5)
    except Exception:
        pass


CSS = """
:root{--bg:#0b1220;--bg2:#0f1a2e;--card:#13203a;--ink:#e8eefc;--muted:#9fb0d0;
--accent:#38bdf8;--accent2:#818cf8;--radius:16px}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink);line-height:1.6}
.wrap{max-width:1000px;margin:0 auto;padding:0 24px}
header{position:sticky;top:0;z-index:50;backdrop-filter:blur(12px);background:rgba(11,18,32,.8);border-bottom:1px solid rgba(255,255,255,.06)}
.nav{display:flex;align-items:center;gap:18px;flex-wrap:wrap;padding:14px 0}
.logo{display:flex;align-items:center;gap:10px;font-weight:700;text-decoration:none;color:var(--ink)}
.logo .dot{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;background:linear-gradient(135deg,var(--accent),var(--accent2));font-size:1.1rem}
.nav a{color:var(--muted);text-decoration:none;font-size:.95rem}
.nav a:hover{color:var(--ink)}
.hero{padding:56px 0 34px}
.kicker{display:inline-block;padding:6px 14px;border-radius:999px;font-size:.8rem;font-weight:600;color:var(--accent);border:1px solid rgba(56,189,248,.35);background:rgba(56,189,248,.08);margin-bottom:16px;letter-spacing:.03em;text-transform:uppercase}
h1{font-size:clamp(1.8rem,5vw,2.8rem);line-height:1.15;font-weight:800;letter-spacing:-.02em;margin-bottom:14px}
h1 span{background:linear-gradient(90deg,var(--accent),var(--accent2));-webkit-background-clip:text;background-clip:text;color:transparent}
.lead{color:var(--muted);font-size:1.05rem;max-width:620px;margin-bottom:22px}
.grid{display:grid;gap:18px;grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}
.card{background:var(--card);border:1px solid rgba(255,255,255,.06);border-radius:var(--radius);padding:24px;transition:transform .18s ease,border-color .18s ease}
.card:hover{transform:translateY(-4px);border-color:rgba(56,189,248,.4)}
.card h3{font-size:1.05rem;margin-bottom:8px}
.card p{color:var(--muted);font-size:.93rem}
.btn{display:inline-block;padding:11px 22px;border-radius:999px;text-decoration:none;font-weight:700;background:linear-gradient(135deg,var(--accent),var(--accent2));color:#04101c;border:1px solid rgba(4,16,28,.35);transition:transform .15s ease,box-shadow .15s ease;font-family:inherit;cursor:pointer}
.btn:hover{transform:translateY(-2px);box-shadow:0 10px 30px rgba(56,189,248,.35)}
.btn.ghost{background:transparent;color:var(--ink);border:1px solid rgba(255,255,255,.2)}
.btn.ghost:hover{box-shadow:none;border-color:var(--accent)}
.btn.sm{padding:7px 14px;font-size:.85rem}
.big{font-size:1.35rem;padding:18px 42px}
.chip{display:inline-flex;gap:8px;align-items:center;padding:10px 16px;border-radius:999px;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.12);cursor:pointer;font-size:.95rem}
.chip input{accent-color:var(--accent)}
.chip:has(input:checked){background:rgba(56,189,248,.14);border-color:rgba(56,189,248,.45)}
.result{background:linear-gradient(135deg,rgba(56,189,248,.14),rgba(129,140,248,.10));border:1px solid rgba(56,189,248,.3);border-radius:20px;padding:32px;text-align:center;margin:22px 0}
.result .name{font-size:2rem;font-weight:800;background:linear-gradient(90deg,var(--accent),var(--accent2));-webkit-background-clip:text;background-clip:text;color:transparent}
.result .hint{color:var(--muted);font-size:.9rem}
input,select{padding:10px 12px;border-radius:10px;border:1px solid rgba(255,255,255,.15);background:rgba(255,255,255,.06);color:var(--ink);font-family:inherit}
select option{background:var(--card);color:var(--ink)}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin:14px 0}
section{padding:30px 0}
h2{font-size:1.3rem;font-weight:800;margin-bottom:10px}
.sub{color:var(--muted);font-size:.92rem;margin-bottom:16px}
.hist{display:grid;gap:6px}
.hist div{display:grid;grid-template-columns:100px 1fr 1fr auto;gap:10px;align-items:center;background:var(--card);border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:8px 12px;font-size:.85rem;color:var(--muted)}
.hist b{color:var(--ink);text-align:left}
footer{padding:26px 0;border-top:1px solid rgba(255,255,255,.06);color:var(--muted);font-size:.85rem;text-align:center}
footer a{color:var(--accent);text-decoration:none;font-weight:600}
@media(max-width:720px){.nav{gap:10px}.nav a:not(.logo){font-size:.85rem;padding:7px 14px;border-radius:999px;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.14);color:var(--ink)}}
@media(max-width:600px){.hist div{grid-template-columns:90px 1fr auto}.hist div span:nth-of-type(2){display:none}}
"""


def nav(g, user):
    code = html.escape(g["code"])
    links = "".join(f"<a href='/g/{code}/{t}'>{LABELS[t]}</a>" for t in TYPES)
    links += f"<a href='/g/{code}/members'>membri</a>"
    if user:
        if user["is_admin"]:
            links += "<a href='/admin'>admin</a>"
        links += "<a href='/logout'>logout</a>"
    return f"""<header><div class="wrap nav">
<a class="logo" href="/"><span class="dot">☕</span> Turnario</a>
{links}</div></header>"""


def doc(title, body):
    return f"""<!doctype html><meta charset=utf-8><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style>{body}"""


def login_page(c, msg=""):
    return doc("Entrare · Turnario", f"""
<main class="wrap hero">
<span class="kicker">Turnario</span>
<h1>Entrare in <span>Turnario!</span></h1>
{f'<p class="sub">{html.escape(msg)}</p>' if msg else ''}
<form method="post" action="/login" class="row">
 <input name="name" placeholder="nome">
 <input name="pin" type="password" placeholder="PIN" inputmode="numeric" autocomplete="off">
 <button class="btn">Entrare</button>
</form>
</main>
<footer>Turnario v{VERSION}</footer>""")


def ledger(c, g, t, user, msg=""):
    code = html.escape(g["code"])
    label = LABELS[t]
    ms = members(c, g["id"])
    ev = today_event(c, g["id"], t)
    present = {r["member_id"] for r in c.execute("SELECT member_id FROM presence WHERE event_id=?", (ev["id"],))} if ev else {m["id"] for m in ms}
    chips = "".join(f"<label class='chip'><input type='checkbox' name='present' value='{m['id']}'"
                    f"{' checked' if m['id'] in present else ''}>{html.escape(m['name'])}</label>" for m in ms)
    st = stats(c, g["id"], t)
    debits = "".join(f"<div class='card'><h3>{html.escape(m['name'])}</h3>"
                     f"<p>presente {st[m['id']][0]} · pagato {st[m['id']][1]}</p>"
                     f"<p class='name' style='font-size:1.4rem;font-weight:800'>{st[m['id']][2]}</p></div>"
                     for m in ms)
    who = "".join(f"<option value='{m['id']}'>{html.escape(m['name'])}</option>"
                  for m in ms if m["id"] in present)
    hist = "".join(
        f"<div><span>{it_date(d)}</span><b>{html.escape(p)}</b><span>{', '.join(map(html.escape, pr)) if pr else '—'}</span>"
        f"{f'<form method=\"post\" action=\"/g/{code}/delete\"><input type=\"hidden\" name=\"id\" value=\"{i}\"><input type=\"hidden\" name=\"type\" value=\"{t}\"><button class=\"btn ghost sm\">elimina</button></form>' if user else ''}</div>"
        for d, p, pr, i in events(c, g["id"], t))
    result = (f"<div class='result'><div class='hint'>oggi {label}</div>"
              f"<div class='name'>{html.escape(ev['name'])}</div>"
              f"<div class='hint'>{it_date(datetime.date.today().isoformat())}</div>"
              f"<form method='post' action='/g/{code}/payer' class='row'>"
              f"<input type='hidden' name='id' value='{ev['id']}'>"
              f"<select name='member'><option value=''>in realtà ha pagato…</option>{who}</select>"
              f"<button class='btn ghost'>corregge</button><a class='btn ghost sm' href='/g/{code}/{t}'>ricarica</a></form></div>") if ev else ""
    form = f"""<div class="card">
<h2>{label}</h2>
<p class="sub">Tutti dentro: togli la spunta agli assenti, poi premi.</p>
<form method="post" action="/g/{code}/event">
<input type="hidden" name="type" value="{t}">
<input type="hidden" name="date" value="{datetime.date.today().isoformat()}">
<div class="row">{chips}</div>
<button class="btn big" type="submit">A chi tocca?</button>
</form>
</div>""" if not ev else f"""<div class="card"><p class="sub">Già registrato oggi.
<form method="post" action="/g/{code}/delete" style="display:inline">
<input type="hidden" name="id" value="{ev['id']}">
<button class="btn ghost">elimina e rifai</button></form></p></div>"""
    if not user:
        form = f"<div class='card'><p class='sub'>Loggati per registrare: <a href='/g/{code}'>entrare</a></p></div>"
    return doc(f"{label} · {g['name']}", f"""{nav(g, user)}
<main class="wrap hero">
<span class="kicker">{html.escape(g['name'])}</span>
<h1><span>{label}</span></h1>
<p class="lead">Sorteggio deterministico: paga chi ha il debito più alto.</p>
{f'<p class="sub">{html.escape(msg)}</p>' if msg else ''}
{result}
{form}
<section><h2>Debiti</h2><div class="grid">{debits}</div></section>
<section><h2>Registro</h2><div class="hist">{hist or '<div><span>vuoto</span></div>'}</div></section>
</main>
<footer>debito = presenze − pagamenti · v{VERSION} · <a href="/">esci</a></footer>
<script>const d = new Date().toISOString().slice(0, 10);
setInterval(() => {{ if (new Date().toISOString().slice(0, 10) !== d) location.reload(); }}, 30000)</script>""")


def members_page(c, g, user, msg=""):
    code = html.escape(g["code"])
    rows = "".join(
        f"<div><span>{html.escape(m['name'])}</span><span>{'attivo' if m['active'] else 'inattivo'} · dal {it_date(m['joined'])}</span>"
        f"<form method='post' action='/g/{code}/toggle'><input type='hidden' name='id' value='{m['id']}'>"
        f"<button class='btn ghost'>{'disattiva' if m['active'] else 'riattiva'}</button></form></div>"
        for m in members(c, g["id"], active_only=False))
    add = ""
    if user and user["is_admin"]:
        add = f"""<form method="post" action="/g/{code}/member" class="row">
<input name="name" placeholder="nome"><input name="pin" type="password" placeholder="PIN 4 cifre" inputmode="numeric">
<button class="btn">Aggiungi</button></form>"""
    return doc(f"Membri · {g['name']}", f"""{nav(g, user)}
<main class="wrap hero">
<span class="kicker">{html.escape(g['name'])}</span>
<h1>Membri <span>{html.escape(g['name'])}</span></h1>
{f'<p class="sub">{html.escape(msg)}</p>' if msg else ''}
{add}
<div class="hist">{rows or '<div><span>nessun membro</span></div>'}</div>
</main>
<footer>Turnario v{VERSION}</footer>""")


def admin_page(c, user, msg=""):
    groups = []
    for g in c.execute("SELECT id,code,name,archived FROM groups ORDER BY id"):
        link = f"<a href='/g/{g['code']}'>/g/{g['code']}</a>" if not g["archived"] else "<span>archiviato</span>"
        groups.append(f"<div><form method='post' action='/admin/rename' class='row'>"
                      f"<input type='hidden' name='id' value='{g['id']}'>"
                      f"<input name='name' value='{html.escape(g['name'])}'>"
                      f"<button class='btn ghost sm'>rinomina</button></form>{link}"
                      f"<form method='post' action='/admin/archive' style='display:inline'>"
                      f"<input type='hidden' name='id' value='{g['id']}'>"
                      f"<button class='btn ghost sm'>{'riattiva' if g['archived'] else 'elimina'}</button></form></div>")
    users = []
    for u in c.execute("SELECT id,name,is_admin FROM users ORDER BY name"):
        users.append(f"<div><span>{html.escape(u['name'])}</span><span>{'admin' if u['is_admin'] else 'utente'}</span>"
                     f"<form method='post' action='/admin/pin' class='row'>"
                     f"<input type='hidden' name='id' value='{u['id']}'>"
                     f"<input name='pin' type='password' placeholder='PIN 4 cifre' inputmode='numeric'>"
                     f"<button class='btn ghost sm'>cambia PIN</button></form>"
                     f"<form method='post' action='/admin/user/delete' style='display:inline'>"
                     f"<input type='hidden' name='id' value='{u['id']}'>"
                     f"<button class='btn ghost sm'>elimina</button></form></div>")
    return doc("Admin", f"""<header><div class="wrap nav">
<a class="logo" href="/"><span class="dot">☕</span> Turnario</a>
<a href="/logout">logout</a></div></header>
<main class="wrap hero">
<span class="kicker">Admin</span>
<h1>Ciao <span>{html.escape(user['name'])}</span></h1>
{f'<p class="sub">{html.escape(msg)}</p>' if msg else ''}
<section><h2>Nuovo gruppo</h2>
<form method="post" action="/admin/group" class="row"><input name="name" placeholder="nome gruppo"><button class="btn">Crea</button></form></section>
<section><h2>Nuovo utente</h2>
<form method="post" action="/admin/user" class="row"><input name="name" placeholder="nome"><input name="pin" type="password" placeholder="PIN 4 cifre" inputmode="numeric"><button class="btn">Crea</button></form></section>
<section><h2>Gruppi</h2><div class="hist">{''.join(groups) or '<div><span>nessuno</span></div>'}</div></section>
<section><h2>Utenti</h2><div class="hist">{''.join(users) or '<div><span>nessuno</span></div>'}</div></section>
</main>
<footer>Turnario v{VERSION} · <a href="/changelog">changelog</a></footer>""")


def landing(c, user, msg=""):
    if not user:
        return login_page(c, msg)
    gs = ([dict(r) for r in c.execute("SELECT code,name FROM groups ORDER BY id")]
          if user["is_admin"] else my_groups(c, user))
    cards = "".join(f"<div class='card'><h3>{html.escape(g['name'])}</h3>"
                    f"<p><a class='btn ghost' href='/g/{g['code']}'>Apri</a></p></div>" for g in gs)
    extra = "<a class='btn' href='/admin'>admin</a>" if user["is_admin"] else ""
    return doc("Turnario", f"""<header><div class="wrap nav">
<a class="logo" href="/"><span class="dot">☕</span> Turnario</a>
<a href="/logout">logout</a></div></header>
<main class="wrap hero"><span class="kicker">Turnario</span>
<h1>Ciao <span>{html.escape(user['name'])}</span></h1>
{f'<p class="sub">{html.escape(msg)}</p>' if msg else ''}
<p class="lead">Scegli il gruppo.</p>
<div class="grid">{cards or '<div class="card"><p>nessun gruppo: chiedilo all\'admin</p></div>'}</div>
<p style="margin-top:20px">{extra}</p></main>
<footer>Turnario v{VERSION} · <a href="/changelog">changelog</a></footer>""")


TRIES = {}  # ponytail: in-process, dies on restart. Use a sqlite table for persistence.
LOCK = threading.Lock()  # one shared sqlite connection across threads: serialize it or
# concurrent requests hit "Recursive use of cursors". House traffic never waits on this.


def throttled(ip):
    n, t = TRIES.get(ip, (0, time.time()))
    if time.time() - t > 300:
        n, t = 0, time.time()
    if n >= 10:
        return True
    TRIES[ip] = (n + 1, t)
    return False


class Handler(BaseHTTPRequestHandler):
    conn = None
    server_version = "Turnario/" + VERSION
    sys_version = ""

    def _send(self, body, code=200, cookie=None):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie:
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            self.send_header("Set-Cookie", f"auth={cookie}; Path=/; Max-Age=31536000; SameSite=Lax{secure}")
        self.end_headers()
        self.wfile.write(body.encode())

    def _redirect(self, path):
        self.send_response(302)
        self.send_header("Location", path)
        self.end_headers()

    def _ip(self):
        """Behind Caddy the socket is 127.0.0.1. Caddy appends the real client to
        X-Forwarded-For, so the last value is the one we can trust."""
        fwd = self.headers.get("X-Forwarded-For", "")
        return (fwd.split(",")[-1].strip() if fwd else "") or self.client_address[0]

    def _error(self, msg, status):
        return self._send(doc(msg, f"""<header><div class="wrap nav">
<a class="logo" href="/"><span class="dot">☕</span> Turnario</a>
<a href="/logout">logout</a></div></header>
<main class="wrap hero"><h1>{html.escape(msg)}</h1>
<p class="sub"><a class="btn ghost" href="/">torna alla home</a></p></main>
<footer>Turnario v{VERSION}</footer>"""), status)

    def _form(self):
        try:
            size = num(self.headers.get("Content-Length", 0))
        except Exception:
            size = 0
        raw = self.rfile.read(min(size, MAX_BODY)).decode("utf-8", "replace")
        q = parse_qs(raw)
        return {k: v[0] for k, v in q.items()}, q

    def do_GET(self):
        with LOCK:
            return self._get()

    def _get(self):
        c, path = self.conn, urlparse(self.path).path
        user = get_user(c, self.headers.get("Cookie", ""))
        if path == "/":
            gs = my_groups(c, user) if user else []
            if len(gs) == 1:
                return self._redirect(f"/g/{gs[0]['code']}")
            return self._send(landing(c, user))
        if path == "/logout":
            return self._send(landing(c, None), 200, ":")
        if path == "/changelog":
            text = open(CHANGELOG, encoding="utf-8").read() if os.path.exists(CHANGELOG) else ""
            return self._send(doc("Changelog · Turnario", f"""<header><div class="wrap nav">
<a class="logo" href="/"><span class="dot">☕</span> Turnario</a>
<a href="/logout">logout</a></div></header>
<main class="wrap hero">
<span class="kicker">Changelog</span>
<h1>Turnario <span>v{VERSION}</span></h1>
<pre style="white-space:pre-wrap">{html.escape(text)}</pre></main>
<footer>Turnario v{VERSION}</footer>"""))
        if path == "/admin":
            if not user or not user["is_admin"]:
                return self._error("Solo l'admin", 403)
            return self._send(admin_page(c, user))
        if path.startswith("/g/"):
            parts = path[3:].split("/")
            g = get_group(c, parts[0])
            if not g:
                return self._error("Gruppo non trovato", 404)
            if not user:
                return self._send(login_page(c, "Entrare per vedere i gruppi."))
            if not (user["is_admin"] or is_member(c, g["id"], user["id"])):
                return self._send(landing(c, user, f"Non sei nel gruppo {g['name']}."), 403)
            if len(parts) > 1 and parts[1] in TYPES:
                return self._send(ledger(c, g, parts[1], user))
            if len(parts) > 1 and parts[1] == "members":
                return self._send(members_page(c, g, user))
            return self._redirect(f"/g/{g['code']}/{TYPES[0]}")
        self._error("404", 404)

    def do_POST(self):
        try:
            size = num(self.headers.get("Content-Length", 0))
        except Exception:
            size = 0
        if size > MAX_BODY:
            return self._error("richiesta troppo grande", 413)
        with LOCK:
            return self._post()

    def _post(self):
        c, path = self.conn, urlparse(self.path).path
        f, q = self._form()
        user = get_user(c, self.headers.get("Cookie", ""))
        if path == "/login":
            ip = self._ip()
            if throttled(ip):
                return self._send(login_page(c, "Troppo tentativi, riprova tra 5 minuti."), 429)
            u = login(c, f.get("name"), f.get("pin"))
            if not u:
                return self._send(login_page(c, "Nome o PIN sbagliati."), 403)
            TRIES.pop(ip, None)
            return self._send(landing(c, u), 200, f"{u['id']}:{u['pin_hash']}")
        if path.startswith("/admin/"):
            if not user or not user["is_admin"]:
                return self._error("Solo l'admin", 403)
            if path == "/admin/group":
                g = create_group(c, f.get("name"))
                if not g:
                    return self._send(admin_page(c, user, "nome vuoto"), 400)
                return self._send(admin_page(c, user, f"gruppo {g['name']} creato: /g/{g['code']}"))
            if path == "/admin/rename":
                name = (f.get("name") or "").strip()[:40]
                if not name:
                    return self._send(admin_page(c, user, "nome vuoto"), 400)
                c.execute("UPDATE groups SET name=? WHERE id=?", (name, num(f.get("id", 0))))
                c.commit()
                return self._send(admin_page(c, user, "gruppo rinominato"))
            if path == "/admin/archive":
                gid = num(f.get("id", 0))
                g = c.execute("SELECT archived FROM groups WHERE id=?", (gid,)).fetchone()
                if not g:
                    return self._send(admin_page(c, user, "gruppo non trovato"), 404)
                c.execute("UPDATE groups SET archived=1-archived WHERE id=?", (gid,))
                c.commit()
                return self._send(admin_page(c, user, "gruppo riattivato" if g["archived"]
                                            else "gruppo eliminato: lo storico resta in archivio"))
            if path == "/admin/user":
                if not PIN_RE.fullmatch(f.get("pin", "")):
                    return self._send(admin_page(c, user, "PIN: 4 cifre"), 400)
                create_user(c, f.get("name"), f["pin"])
                return self._send(admin_page(c, user, "utente creato: comunica il PIN"))
            if path == "/admin/pin":
                if not PIN_RE.fullmatch(f.get("pin", "")):
                    return self._send(admin_page(c, user, "PIN: 4 cifre"), 400)
                u = c.execute("SELECT id FROM users WHERE id=?", (num(f.get("id", 0)),)).fetchone()
                if not u:
                    return self._send(admin_page(c, user, "utente non trovato"), 404)
                salt = secrets.token_hex(8)
                c.execute("UPDATE users SET pin_salt=?, pin_hash=? WHERE id=?",
                          (salt, hash_pin(f["pin"], salt), u["id"]))
                c.commit()
                return self._send(admin_page(c, user, "PIN cambiato"
                                            + (" (rifai il login)" if u["id"] == user["id"] else "")))
            if path == "/admin/user/delete":
                uid = num(f.get("id", 0))
                u = c.execute("SELECT is_admin FROM users WHERE id=?", (uid,)).fetchone()
                if not u:
                    return self._send(admin_page(c, user, "utente non trovato"), 404)
                if u["is_admin"] and not c.execute("SELECT 1 FROM users WHERE is_admin=1 AND id<>?", (uid,)).fetchone():
                    return self._send(admin_page(c, user, "non puoi eliminare l'ultimo admin"), 400)
                delete_user(c, uid)
                return self._send(admin_page(c, user, "utente eliminato (con i suoi eventi)"))
            return self._error("404", 404)
        if not path.startswith("/g/"):
            return self._error("404", 404)
        parts = path[3:].split("/")
        g = get_group(c, parts[0])
        if not g:
            return self._error("Gruppo non trovato", 404)
        if not user:
            return self._send(login_page(c, "Serve il login."), 403)
        if not (user["is_admin"] or is_member(c, g["id"], user["id"])):
            return self._send(landing(c, user, f"Non sei nel gruppo {g['name']}."), 403)
        t = f.get("type") if f.get("type") in TYPES else TYPES[0]
        if path.endswith("/event"):
            date = f.get("date") if valid_date(f.get("date")) else datetime.date.today().isoformat()
            present = {num(x) for x in q.get("present", [])}
            eid = record(c, g["id"], t, date, present)
            if eid:
                who = c.execute("SELECT u.name FROM events e JOIN members m ON m.id=e.member_id "
                                "JOIN users u ON u.id=m.user_id WHERE e.id=?", (eid,)).fetchone()["name"]
                ping(f"{g['name']} · {LABELS[t]}: {who}")
            msg = "registrato" if eid else ("già registrato oggi" if today_event(c, g["id"], t) else "nessuno è presente: spunta almeno una persona")
            return self._send(ledger(c, g, t, user, msg), 200 if eid else 409)
        if path.endswith("/payer"):
            eid, mid = num(f.get("id", 0)), num(f.get("member", 0))
            if not c.execute("SELECT 1 FROM presence p JOIN events e ON e.id=p.event_id "
                             "WHERE p.event_id=? AND p.member_id=? AND e.group_id=?",
                             (eid, mid, g["id"])).fetchone():
                return self._send(ledger(c, g, t, user, "seleziona un pagatore presente"), 400)
            c.execute("UPDATE events SET member_id=? WHERE group_id=? AND id=?", (mid, g["id"], eid))
            c.commit()
            return self._send(ledger(c, g, t, user, "pagatore corretto"))
        if path.endswith("/delete"):
            delete_event(c, g["id"], num(f.get("id", 0)))
            return self._send(ledger(c, g, t, user, "evento eliminato"))
        if path.endswith("/member"):
            if not user["is_admin"]:
                return self._send(members_page(c, g, user, "Solo l'admin aggiunge membri"), 403)
            if not PIN_RE.fullmatch(f.get("pin", "")):
                return self._send(members_page(c, g, user, "PIN: 4 cifre"), 400)
            uid = create_user(c, f.get("name"), f["pin"])
            add_member(c, g["id"], uid)
            return self._send(members_page(c, g, user, f"{(f.get('name') or '').strip()[:40]} aggiunto"))
        if path.endswith("/toggle"):
            if not user["is_admin"]:
                return self._send(members_page(c, g, user, "Solo l'admin cambia membri"), 403)
            c.execute("UPDATE members SET active=1-active WHERE group_id=? AND id=?", (g["id"], num(f.get("id", 0))))
            c.commit()
            return self._send(members_page(c, g, user, "membro aggiornato"))
        self._error("404", 404)

    def log_message(self, *a):
        pass


def test():
    c = db(":memory:")
    elena = create_user(c, "Elena", "1234")
    assert create_user(c, "Elena", "9999") != elena
    assert c.execute("SELECT name FROM users WHERE id=?", (create_user(c, "Elena", "9999"),)).fetchone()["name"] == "Elena3"
    create_user(c, "boss", "0000", admin=True)
    g = create_group(c, "Amici")
    assert login(c, "Elena", "1234") and not login(c, "Elena", "0000")
    h = c.execute("SELECT pin_hash FROM users WHERE id=?", (elena,)).fetchone()["pin_hash"]
    assert get_user(c, f"auth={elena}:{h}")["name"] == "Elena"
    assert get_user(c, f"auth={elena}:wrong") is None
    add_member(c, g["id"], elena)
    occ = create_user(c, "Occasional", "1111")
    add_member(c, g["id"], occ)
    ms = members(c, g["id"])
    reg = next(m["id"] for m in ms if m["name"] == "Elena")
    occ = next(m["id"] for m in ms if m["name"] == "Occasional")
    d, paid = datetime.date(2026, 1, 1), {reg: 0, occ: 0}
    for i in range(20):
        present = {reg} | ({occ} if i in (2, 6, 10) else set())
        eid = record(c, g["id"], "coffee", (d + datetime.timedelta(days=i)).isoformat(), present)
        paid[c.execute("SELECT member_id FROM events WHERE id=?", (eid,)).fetchone()["member_id"]] += 1
    assert 1 <= paid[occ] <= 3, paid
    assert paid[reg] == 20 - paid[occ], paid
    assert record(c, g["id"], "coffee", d.isoformat(), {reg}) is None
    assert record(c, g["id"], "coffee", (d + datetime.timedelta(days=21)).isoformat(), set()) is None
    eid = record(c, g["id"], "coffee", (d + datetime.timedelta(days=20)).isoformat(), {reg, occ})
    c.execute("UPDATE events SET member_id=? WHERE id=?", (occ, eid))
    c.commit()
    assert c.execute("SELECT member_id FROM events WHERE id=?", (eid,)).fetchone()["member_id"] == occ
    for i in range(4):
        record(c, g["id"], "car", (d + datetime.timedelta(days=30 + i)).isoformat(), {reg, occ})
    debts = [stats(c, g["id"], "car")[m][2] for m in (reg, occ)]
    assert max(debts) - min(debts) <= 1, debts
    assert pick([reg, occ], {reg: 0, occ: 0}) in (reg, occ)
    assert pick([reg, occ], {reg: 1, occ: 5}) == occ
    c.execute("UPDATE members SET active=0 WHERE id=?", (occ,))
    c.commit()
    assert occ not in stats(c, g["id"], "car")
    assert my_groups(c, {"id": elena})[0]["name"] == "Amici"
    assert is_member(c, g["id"], elena) and not is_member(c, g["id"], 999)
    assert login(c, "elena", "1234") is None
    c.execute("UPDATE groups SET name='Amici di sempre' WHERE id=?", (g["id"],))
    c.commit()
    assert my_groups(c, {"id": elena})[0]["name"] == "Amici di sempre"
    c.execute("UPDATE groups SET archived=1 WHERE id=?", (g["id"],))
    c.commit()
    assert get_group(c, g["code"]) is None and not my_groups(c, {"id": elena})
    assert c.execute("SELECT COUNT(*) FROM events WHERE group_id=?", (g["id"],)).fetchone()[0] > 0
    c.execute("UPDATE groups SET archived=0 WHERE id=?", (g["id"],))
    c.commit()
    assert get_group(c, g["code"])
    ghost = create_user(c, "Ghost", "1111")
    add_member(c, g["id"], ghost)
    gm = next(m["id"] for m in members(c, g["id"]) if m["name"] == "Ghost")
    before = c.execute("SELECT COUNT(*) FROM events WHERE group_id=?", (g["id"],)).fetchone()[0]
    record(c, g["id"], "car", (d + datetime.timedelta(days=60)).isoformat(), {gm, reg})
    delete_user(c, ghost)
    assert c.execute("SELECT COUNT(*) FROM users WHERE id=?", (ghost,)).fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM events WHERE group_id=?", (g["id"],)).fetchone()[0] == before
    assert c.execute("SELECT COUNT(*) FROM members WHERE group_id=?", (g["id"],)).fetchone()[0] == 2
    assert it_date("2026-10-07") == "7 ottobre 2026"
    assert "A chi tocca?" in ledger(c, g, "coffee", {"name": "Elena", "is_admin": 0})
    assert "class='result'" not in ledger(c, g, "coffee", {"name": "Elena", "is_admin": 0})
    assert "A chi tocca?" not in ledger(c, g, "coffee", None)
    record(c, g["id"], "coffee", datetime.date.today().isoformat(), {reg})
    page = ledger(c, g, "coffee", {"name": "Elena", "is_admin": 0})
    assert "class='result'" in page and "elimina e rifai" in page and "A chi tocca?" not in page
    assert num("x") == 0 and num("7") == 7
    assert valid_date("2026-10-07") and not valid_date("2026-13-01") and not valid_date(None)
    assert it_date("garbage") == "garbage"
    fake = create_user(c, "<b>x</b>", "1111")
    add_member(c, g["id"], fake)
    fid = next(m["id"] for m in members(c, g["id"]) if m["name"] == "<b>x</b>")
    record(c, g["id"], "car", (d + datetime.timedelta(days=90)).isoformat(), {fid, reg})
    assert "&lt;b&gt;x&lt;/b&gt;" in ledger(c, g, "car", {"name": "Elena", "is_admin": 0})
    assert not throttled("1.2.3.4")
    for _ in range(10):
        throttled("1.2.3.4")
    assert throttled("1.2.3.4")
    assert create_group(c, "   ") is None
    assert it_date(None) is None
    assert "nota di prova" in members_page(c, g, {"name": "Elena", "is_admin": 0}, "nota di prova")
    print("ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default="turnario.db")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--admin", nargs=2, metavar=("NAME", "PIN"), help="create an administrator")
    ap.add_argument("--version", action="version", version=f"Turnario {VERSION}")
    a = ap.parse_args()
    if a.admin:
        if not PIN_RE.fullmatch(a.admin[1] or ""):
            raise SystemExit("PIN: 4 cifre")
        c = db(a.db)
        uid = create_user(c, a.admin[0], a.admin[1], admin=True)
        print(f"admin #{uid}: {a.admin[0]}")
    elif a.test:
        test()
    else:
        print(f"Turnario {VERSION} on 127.0.0.1:{a.port}")
        Handler.conn = db(a.db)
        ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
