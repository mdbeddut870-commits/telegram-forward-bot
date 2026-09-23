#!/usr/bin/env python3
"""Mini web dashboard (stdlib only) - served by bot.py's health server.

    GET  /                          HTML dashboard (token protected)
    GET  /api/stats?days=N          JSON stats overview
    GET  /api/mappings              JSON mapping list
    GET  /api/seen                  JSON recent dedup entries
    POST /api/mappings/<id>/toggle  pause / resume a mapping

Auth: set DASHBOARD_TOKEN; open /?token=YOUR_TOKEN once -> session cookie.
/ping and /health stay public (Railway probes).
"""

import html
import json
import re
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

import config
import database as db


def _send(handler, code, body, ctype="text/html; charset=utf-8", extra=None):
    if isinstance(body, str):
        body = body.encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    for key, value in (extra or []):
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def _json(handler, code, payload):
    _send(handler, code, json.dumps(payload), "application/json; charset=utf-8")

_CSS = (
    "*{box-sizing:border-box}"
    "body{margin:0;background:#0f1419;color:#e6edf3;font:14px/1.5 sans-serif}"
    "header{display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;padding:14px 24px;border-bottom:1px solid #2d333b}"
    "h1{font-size:17px;margin:0}h2{font-size:13px;margin:22px 0 8px;color:#8b949e;text-transform:uppercase}"
    ".wrap{padding:18px 24px;max-width:1240px;margin:0 auto}"
    ".cards{display:flex;gap:12px;flex-wrap:wrap}"
    ".card{background:#1a2028;border:1px solid #2d333b;border-radius:10px;padding:12px 18px;min-width:130px}"
    ".card .n{font-size:24px;font-weight:700}.card .l{font-size:10px;color:#8b949e;margin-top:2px}"
    ".good{color:#3fb950}.bad{color:#f85149}.warn{color:#d29922}"
    "table{width:100%;border-collapse:collapse;background:#1a2028;border:1px solid #2d333b}"
    "th,td{padding:7px 12px;text-align:left;font-size:13px;border-bottom:1px solid #2d333b}"
    "th{background:#21262d;color:#8b949e;font-weight:600}tr:last-child td{border-bottom:none}"
    ".badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px}"
    ".on{background:#1f6feb33;color:#58a6ff}.off{background:#6e768133;color:#8b949e}"
    ".btn{border:0;border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer;color:#fff}"
    ".btn.pause{background:#6e7681}.btn.resume{background:#238636}"
    "a{color:#58a6ff;text-decoration:none}.muted{color:#8b949e;font-size:12px}"
    "code{background:#21262d;padding:1px 5px;border-radius:4px}"
    ".deny{max-width:640px;margin:60px auto;background:#1a2028;border:1px solid #2d333b;border-radius:10px;padding:26px}"
)


def _page(title, body):
    doc = ("<!doctype html><html><head><meta charset='utf-8'>"
           "<meta name='viewport' content='width=device-width,initial-scale=1'>"
           "<meta http-equiv='refresh' content='30'>"
           "<title>" + html.escape(title) + "</title><style>" + _CSS + "</style></head><body>"
           + body + "</body></html>")
    return doc.encode("utf-8")


def _deny(title, lines):
    inner = "".join("<p>" + ln + "</p>" for ln in lines)
    return _page(title, "<div class='deny'><h1>" + html.escape(title) + "</h1>" + inner + "</div>")


def _auth(handler, query):
    token = (config.DASHBOARD_TOKEN or "").strip()
    if not token:
        return "disabled"
    provided = []
    try:
        provided.extend(v for v in parse_qs(query).get("token", []) if v)
    except Exception:
        pass
    raw = handler.headers.get("Cookie") or ""
    if raw:
        try:
            jar = SimpleCookie()
            jar.load(raw)
            if "dash_token" in jar:
                provided.append(jar["dash_token"].value)
        except Exception:
            pass
    auth_header = handler.headers.get("Authorization") or ""
    if auth_header.startswith("Bearer "):
        provided.append(auth_header[7:].strip())
    return "ok" if token in provided else "denied"


def _login_redirect(handler, token):
    handler.send_response(303)
    handler.send_header("Location", "/")
    handler.send_header("Set-Cookie",
        "dash_token=" + token + "; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800",
    )
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _parse_days(query):
    try:
        return max(1, min(90, int(parse_qs(query).get("days", ["7"])[0])))
    except Exception:
        return 7


def _fmt_uptime(started):
    if not started:
        return "unknown"
    try:
        st = datetime.fromisoformat(started)
        if st.tzinfo is None:
            st = st.replace(tzinfo=timezone.utc)
        secs = max(0, int((datetime.now(timezone.utc) - st).total_seconds()))
    except Exception:
        return "unknown"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return "%dd %dh %dm" % (d, h, m)
    if h:
        return "%dh %dm" % (h, m)
    return "%dm %ds" % (m, s)


def _render(stats, mappings, seen, days, started_at):
    now = datetime.now(timezone.utc)
    names = {m["id"]: m.get("source_name") or str(m["source_id"]) for m in mappings}
    active = sum(1 for m in mappings if m["active"])
    top_sorted = sorted(stats["top"], key=lambda r: r["f"], reverse=True)[:10]
    top_max = top_sorted[0]["f"] if top_sorted else 1

    entries = [
        ("FORWARDED %dd" % days, str(stats["forwarded"]), ""),
        ("FAILED %dd" % days, str(stats["failed"]), ""),
        ("DEDUP %dd" % days, str(stats["dedup_skip"]), ""),
        ("MAPPINGS", str(len(mappings)), ""),
        ("ACTIVE", str(active), "good"),
        ("UPTIME", html.escape(_fmt_uptime(started_at)), ""),
    ]
    cards = "".join(
        "<div class='card'><div class='n %s'>%s</div><div class='l'>%s</div></div>"
        % (cls, val, label) for label, val, cls in entries)

    head = ("<header><h1>Telegram Forward Bot &mdash; Dashboard</h1>"
            "<span class='muted'>updated " + now.strftime("%Y-%m-%d %H:%M:%S UTC")
            + " &middot; auto-refresh 30s &middot; <a href='/ping'>/ping</a>"
            " <a href='/health'>/health</a></span></header>")

    day_row = "".join(
        "<tr><td>%s</td><td>%d</td><td>%d</td><td>%d</td></tr>"
        % (v.get("day", "-"), v.get("f", 0), v.get("fa", 0), v.get("d", 0))
        for v in stats["per_day"]
    )
    day_tbl = ("<table><tr><th>Day</th><th>Forwarded</th><th>Failed</th><th>Dedup</th></tr>"
               + (day_row or "<tr><td colspan='4' class='muted'>no data</td></tr>") + "</table>")

    top_rows = ""
    if top_sorted:
        for row in top_sorted:
            cnt = row["f"]
            label = names.get(row["mapping_id"], str(row["mapping_id"]))
            width = max(4, int(240 * cnt / top_max)) if top_max else 4
            bar = "<span class='bar' style='width:%dpx'></span> " % width
            top_rows += "<tr><td>%s %s</td><td>%d</td></tr>" % (bar, html.escape(label), cnt)
    else:
        top_rows = "<tr><td class='muted'>no data</td></tr>"
    top_tbl = "<table><tr><th>Mapping</th><th>Forwarded</th></tr>" + top_rows + "</table>"

    map_rows = ""
    for m in mappings:
        src = html.escape("%s %s" % (m["source_id"], m.get("source_name") or ""))
        dst = html.escape("%s %s" % (m["dest_id"], m.get("dest_name") or ""))
        status = ("<span class='badge on'>active</span>" if m["active"]
                  else "<span class='badge off'>paused</span>")
        act = (("<form method='post' action='/api/mappings/%d/toggle' style='display:inline'>"
                "<button class='btn pause'>pause</button></form>") if m["active"] else
               ("<form method='post' action='/api/mappings/%d/toggle' style='display:inline'>"
                "<button class='btn resume'>resume</button></form>")) % m["id"]
        map_rows += ("<tr><td>%d</td><td>%s</td><td>%s</td><td>%s %s</td></tr>"
                     % (m["id"], src, dst, status, act))
    map_tbl = ("<table><tr><th>#</th><th>Source</th><th>Destination</th><th>Status</th></tr>"
               + (map_rows or "<tr><td colspan='4' class='muted'>no mappings</td></tr>") + "</table>")

    seen_rows = ""
    for row in seen:
        seen_rows += ("<tr><td class='muted'>%s</td><td><code>%s</code></td><td class='muted'>%s</td></tr>"
                      % (html.escape((row["first_seen"] or "")[:19]),
                         html.escape(row["content_hash"][:36]),
                         html.escape(str(row["source_id"]))))
    seen_tbl = ("<table><tr><th>Time (UTC)</th><th>Content hash</th><th>Source</th></tr>"
                + (seen_rows or "<tr><td colspan='3' class='muted'>no entries in window</td></tr>")
                + "</table>")

    nav = ("<p class='muted'>JSON: <a href='/api/stats'>/api/stats</a> "
           "<a href='/api/mappings'>/api/mappings</a> <a href='/api/seen'>/api/seen</a>"
           " &middot; %d active / %d mappings &middot; days=%d "
           "(<a href='/?days=1'>1d</a> <a href='/?days=7'>7d</a> <a href='/?days=30'>30d</a>)</p>"
           % (active, len(mappings), days))

    body = ("<div class='wrap'>" + head
            + "<h2>Overview</h2><div class='cards'>" + cards + "</div>"
            + nav
            + "<h2>Forwarded / failed / dedup per day</h2>" + day_tbl
            + "<h2>Top mappings</h2>" + top_tbl
            + "<h2>Mappings (pause / resume)</h2>" + map_tbl
            + "<h2>Recent dedup entries</h2>" + seen_tbl
            + "<p class='muted'>logout: <a href='/logout'>/logout</a></p></div>")
    return _page("Forward Bot Dashboard", body)


def handle_get(handler, path, query):
    if path == "/logout":
        handler.send_response(303)
        handler.send_header("Location", "/")
        handler.send_header("Set-Cookie", "dash_token=; Path=/; Max-Age=0")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return

    qs = parse_qs(query)
    token = (config.DASHBOARD_TOKEN or "").strip()
    if path in ("/", "/index.html") and qs.get("token"):
        if token and token in qs.get("token", []):
            _login_redirect(handler, token)
        else:
            _send(handler, 403, _deny("403 - bad token", [
                "The supplied token does not match DASHBOARD_TOKEN."]))
        return

    if path in ("/api/stats", "/api/mappings", "/api/seen"):
        if _auth(handler, query) != "ok":
            _json(handler, 401, {"error": "unauthorized"})
            return
        if path == "/api/stats":
            _json(handler, 200, db.get_stats(_parse_days(query)))
        elif path == "/api/mappings":
            _json(handler, 200, {"mappings": db.list_mappings()})
        else:
            _json(handler, 200, {"seen": db.recent_seen(30)})
        return

    state = _auth(handler, query)
    if state == "disabled":
        _send(handler, 403, _deny("Dashboard disabled", [
            "Set the <code>DASHBOARD_TOKEN</code> environment variable,",
            "redeploy, then open <code>/?token=YOUR_TOKEN</code>."]))
        return
    if state == "denied":
        _send(handler, 403, _deny("403 - token required", [
            "Open <code>/?token=YOUR_TOKEN</code> once to log in."]))
        return
    days = _parse_days(query)
    _send(handler, 200, _render(db.get_stats(days), db.list_mappings(),
                                db.recent_seen(30), days, db.get_state("started_at")))


_TOGGLE_RE = re.compile(r"^/api/mappings/(\d+)/toggle$")


def handle_post(handler, path, query):
    match = _TOGGLE_RE.match(path)
    if not match:
        _json(handler, 404, {"error": "not found"})
        return
    if _auth(handler, query) != "ok":
        _json(handler, 401, {"error": "unauthorized"})
        return
    mapping_id = int(match.group(1))
    active = db.toggle_mapping(mapping_id)
    if active is None:
        _json(handler, 404, {"error": "mapping not found"})
        return
    if "json" in (handler.headers.get("Accept") or ""):
        _json(handler, 200, {"id": mapping_id, "active": bool(active)})
        return
    handler.send_response(303)
    handler.send_header("Location", "/")
    handler.send_header("Content-Length", "0")
    handler.end_headers()
