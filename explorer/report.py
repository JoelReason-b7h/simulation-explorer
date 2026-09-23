"""Writes the run as a single self-contained page, rewritten after every step.

The page reloads itself, so leaving it open in a browser shows a run as it happens. It carries no
external references, because the data lives on this machine and nothing should have to serve it.
"""

from __future__ import annotations

import html
import json
from datetime import datetime

PALETTE = """
:root{--ground:#FAFBFA;--surface:#EFF3F0;--surface-2:#E2EAE5;--ink:#101E19;--muted:#53645C;
--line:#D2DDD7;--accent:#0E4A3C;--accent-soft:#DBEAE3;--pending:#8A5A12;--pending-soft:#F4EBD8;
--active:#3F7F63;--active-soft:#DCEBE1;--fail:#A33734;--fail-soft:#F7E3E2;--gone:#6A6F6C;
--gone-soft:#ECEFED;
--font-sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
--font-mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
--step--2:0.8125rem;--step--1:0.9375rem;--step-0:1.125rem;--step-1:1.3125rem;--step-2:1.5625rem;
--leading:1.6}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--ground:#0D1512;
--surface:#15201C;--surface-2:#1E2B25;--ink:#E3EBE6;--muted:#92A49A;--line:#26332D;--accent:#5FC2A3;
--accent-soft:#15302A;--pending:#DFA24E;--pending-soft:#2C2617;--active:#6FC79E;--active-soft:#15281F;
--fail:#E38480;--fail-soft:#2C1B1B;--gone:#868C89;--gone-soft:#1F2320}}
:root[data-theme="dark"]{--ground:#0D1512;--surface:#15201C;--surface-2:#1E2B25;--ink:#E3EBE6;
--muted:#92A49A;--line:#26332D;--accent:#5FC2A3;--accent-soft:#15302A;--pending:#DFA24E;
--pending-soft:#2C2617;--active:#6FC79E;--active-soft:#15281F;--fail:#E38480;--fail-soft:#2C1B1B;
--gone:#868C89;--gone-soft:#1F2320}
"""

STYLE = """
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--font-sans);
font-size:var(--step-0);line-height:var(--leading);padding:2rem 1rem 4rem}
.wrap{max-width:1100px;margin:0 auto}
h1{font-size:var(--step-2);margin:0 0 .25rem;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:var(--step--1);margin:0 0 2rem}
.live{display:inline-block;width:.5rem;height:.5rem;border-radius:50%;background:var(--active);
margin-right:.4rem;vertical-align:middle}
h2{font-size:var(--step-1);margin:2.5rem 0 .75rem;padding-left:.7rem;
border-left:3px solid var(--accent)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.75rem;
margin-bottom:.5rem}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:.9rem 1rem}
.tile .n{font-size:var(--step-2);font-variant-numeric:tabular-nums;line-height:1.1}
.tile .l{color:var(--muted);font-size:var(--step--2);text-transform:uppercase;letter-spacing:.06em;
margin-top:.3rem}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:var(--step--1)}
th{text-align:left;font-weight:600;color:var(--muted);font-size:var(--step--2);
text-transform:uppercase;letter-spacing:.06em;padding:.6rem .8rem;border-bottom:1px solid var(--line);
white-space:nowrap}
td{padding:.55rem .8rem;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}
code,.mono{font-family:var(--font-mono);font-size:var(--step--1)}
.pill{display:inline-block;padding:.1rem .5rem;border-radius:999px;font-size:var(--step--2);
font-weight:600;white-space:nowrap}
.ok{background:var(--active-soft);color:var(--active)}
.rej{background:var(--fail-soft);color:var(--fail)}
.state{font-family:var(--font-mono);font-size:var(--step--1);color:var(--ink)}
.chg{color:var(--muted);font-size:var(--step--2)}
.chg b{color:var(--accent);font-weight:600}
.bar{height:.4rem;background:var(--surface-2);border-radius:999px;overflow:hidden;min-width:60px}
.bar i{display:block;height:100%;background:var(--accent)}
.rule{background:var(--fail-soft);border:1px solid var(--line);border-left:3px solid var(--fail);
border-radius:8px;padding:.7rem .9rem;margin-bottom:.6rem}
.rule .msg{font-family:var(--font-mono);font-size:var(--step--1)}
.rule .who{color:var(--muted);font-size:var(--step--2);margin-top:.25rem}
.empty{color:var(--muted);padding:1rem;font-size:var(--step--1)}
"""


def _state(key):
    if not isinstance(key, (list, tuple)):
        return html.escape(str(key))
    parts = [html.escape(str(p)) for p in key]
    return parts[0] + " · " + " / ".join(parts[1:]) if len(parts) > 1 else parts[0]


def render(run_id, base_url, platform_uid, trials, summary, frontier, finished):
    rows = []
    for t in reversed(trials):
        pill = '<span class="pill ok">%s</span>' % t["status"] if t["ok"] \
            else '<span class="pill rej">%s</span>' % t["status"]
        changes = ", ".join("<b>%s</b>" % html.escape(c) for c in t["changes"]) or "no change"
        rows.append(
            "<tr><td class='mono'>%d</td><td><strong>%s</strong></td><td>%s</td>"
            "<td class='state'>%s</td><td class='chg'>%s</td></tr>"
            % (t["n"], html.escape(t["action"]), pill, _state(t["key"]), changes))

    most = max([f["visits"] for f in frontier] or [1])
    front = []
    for f in frontier:
        width = int(100 * f["visits"] / most)
        front.append(
            "<tr><td class='state'>%s</td>"
            "<td><div class='bar'><i style='width:%d%%'></i></div></td>"
            "<td class='mono'>%d</td><td class='chg'>%s</td></tr>"
            % (_state(f["key"]), width, f["visits"],
               ", ".join(html.escape(u) for u in f["untried"])))

    rules = [
        "<div class='rule'><div class='msg'>%s</div><div class='who'>found by %s</div></div>"
        % (html.escape(r["message"]), html.escape(r["action"]))
        for r in _rules(trials)
    ] or ["<div class='empty'>Nothing rejected yet.</div>"]

    tiles = "".join(
        "<div class='tile'><div class='n'>%s</div><div class='l'>%s</div></div>" % (v, html.escape(k))
        for k, v in summary.items())

    refresh = "" if finished else '<meta http-equiv="refresh" content="2">'
    status = "finished" if finished else '<span class="live"></span>running'

    return """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
%s<title>Direct explorer — %s</title><style>%s%s</style></head><body><div class="wrap">
<h1>Direct API explorer</h1>
<p class="sub">run <code>%s</code> · %s · platform <code>%s</code> · %s · updated %s</p>
<div class="tiles">%s</div>
<h2>What it tried, newest first</h2>
<div class="scroll"><table>
<thead><tr><th>#</th><th>Action</th><th>Result</th><th>State it acted on</th><th>What changed</th></tr></thead>
<tbody>%s</tbody></table></div>
<h2>Where it has not been</h2>
<div class="scroll"><table>
<thead><tr><th>State</th><th>Visits</th><th></th><th>Untried actions</th></tr></thead>
<tbody>%s</tbody></table></div>
<h2>Rules it discovered</h2>
%s
</div></body></html>""" % (
        refresh, html.escape(run_id), PALETTE, STYLE, html.escape(run_id), html.escape(base_url),
        html.escape(platform_uid or "-"), status, datetime.now().strftime("%H:%M:%S"),
        tiles, "".join(rows) or "<tr><td colspan='5' class='empty'>No trials yet.</td></tr>",
        "".join(front) or "<tr><td colspan='4' class='empty'>Frontier empty.</td></tr>",
        "".join(rules))


def _rules(trials):
    """A rejection is an observation: the message is the rule the service enforces."""
    seen, out = set(), []
    for t in trials:
        if t["ok"] or not t.get("message"):
            continue
        if t["message"] in seen:
            continue
        seen.add(t["message"])
        out.append({"message": t["message"], "action": t["action"]})
    return out


def write(path, **kwargs):
    with open(path, "w") as handle:
        handle.write(render(**kwargs))
