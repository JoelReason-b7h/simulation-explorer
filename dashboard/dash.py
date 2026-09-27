#!/usr/bin/env python3
"""Health page for the mini PC that runs the local stack and the simulation harness.

    python3 dashboard/dash.py   # serves http://127.0.0.1:8440; `tailscale serve --bg 8440` exposes it
                                # to the tailnet only

GET /           the page, refreshed every 30 seconds
GET /api/status the same data as JSON
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HARNESS_DIR = Path(os.environ.get("HARNESS_DIR", Path(__file__).resolve().parent.parent))
OPS_HEALTH = os.environ.get("OPS_HEALTH", "http://localhost:5200/health")
PORT = int(os.environ.get("DASH_PORT", "8440"))
CACHE_SECONDS = 10
CYCLE_GRACE_SECONDS = 600

STACK = ("adapter", "clearing", "compliance", "compliance-api", "core", "core-ro",
         "hot-sauce-bank", "localstack", "ops-api", "postgres", "public-api",
         "redis", "simulator-api", "toxiproxy", "wiremock")


def run(args, timeout=15):
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return done.returncode, done.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def meminfo():
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        values[key] = int(rest.split()[0]) * 1024
    return values


def cpu_temp():
    temps = []
    for zone in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            temps.append(int(zone.read_text()) / 1000)
        except (OSError, ValueError):
            pass
    return max(temps) if temps else None


def system():
    mem = meminfo()
    disk = shutil.disk_usage("/")
    load = os.getloadavg()
    uptime = float(Path("/proc/uptime").read_text().split()[0])
    return {
        "cores": os.cpu_count(),
        "load": [round(x, 2) for x in load],
        "memTotal": mem["MemTotal"],
        "memAvailable": mem["MemAvailable"],
        "swapTotal": mem["SwapTotal"],
        "swapUsed": mem["SwapTotal"] - mem["SwapFree"],
        "diskTotal": disk.total,
        "diskUsed": disk.used,
        "tempC": cpu_temp(),
        "uptimeSeconds": int(uptime),
    }


def containers():
    code, out = run(["docker", "ps", "-a", "--filter", "name=docker-", "--format", "{{.Names}}"])
    if code != 0:
        return None
    names = [n for n in out.split() if n.startswith("docker-") and n.endswith("-1")]
    found = {}
    if names:
        _, raw = run(["docker", "inspect"] + names)
        for c in json.loads(raw or "[]"):
            name = c["Name"].lstrip("/")[len("docker-"):-len("-1")]
            state = c["State"]
            found[name] = {
                "state": state["Status"],
                "health": (state.get("Health") or {}).get("Status"),
                "restarts": c.get("RestartCount", 0),
                "oomKilled": state.get("OOMKilled", False),
                "exitCode": state.get("ExitCode"),
                "startedAt": state.get("StartedAt", "")[:19].replace("T", " "),
            }
        _, stats = run(["docker", "stats", "--no-stream", "--format",
                        "{{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}"] + names, timeout=20)
        for line in stats.splitlines():
            parts = line.split("\t")
            if len(parts) == 4:
                name = parts[0][len("docker-"):-len("-1")]
                if name in found:
                    found[name].update(mem=parts[1], memPerc=parts[2], cpu=parts[3])
    for name in STACK:
        found.setdefault(name, {"state": "absent"})
    return dict(sorted(found.items()))


def ops_health():
    try:
        return urllib.request.urlopen(OPS_HEALTH, timeout=5).status
    except Exception:  # noqa: BLE001 - any failure means unhealthy
        return None


def tail(path, lines=15):
    try:
        return path.read_text(errors="replace").splitlines()[-lines:]
    except OSError:
        return []


def open_findings():
    try:
        text = (HARNESS_DIR / "FINDINGS.md").read_text()
    except OSError:
        return None
    section = text.split("## Open", 1)[-1].split("\n## ", 1)[0]
    return len(re.findall(r"^\d+\. ", section, re.M))


def harness():
    if not HARNESS_DIR.is_dir():
        return {"installed": False, "dir": str(HARNESS_DIR)}
    _, pids = run(["pgrep", "-f", "fleet_loop.running.sh|fleet_loop.sh"])
    events = []
    for line in tail(HARNESS_DIR / "fleet.progress", 400):
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    starts = {e["name"]: e for e in events if "started" in e}
    ends = [e for e in events if "runs" in e]
    current = None
    if events and "started" in events[-1]:
        e = events[-1]
        started = datetime.strptime(e["started"], "%Y-%m-%d %H:%M:%S")
        elapsed = int((datetime.now() - started).total_seconds())
        current = {"name": e["name"], "started": e["started"], "seconds": e["seconds"],
                   "elapsed": elapsed, "overdue": elapsed > e["seconds"] + CYCLE_GRACE_SECONDS}
    recent = []
    for e in reversed(ends[-10:]):
        runs = e["runs"].values()
        start = starts.get(e["name"], {}).get("started")
        took = None
        if start:
            took = int((datetime.strptime(e["at"], "%Y-%m-%d %H:%M:%S")
                        - datetime.strptime(start, "%Y-%m-%d %H:%M:%S")).total_seconds())
        recent.append({
            "name": e["name"], "at": e["at"], "took": took,
            "trials": sum(r.get("trials", 0) for r in runs),
            "violations": sum(r.get("violations", 0) for r in runs),
            "serviceErrors": e.get("serviceErrors"),
            "newRules": e.get("newRules", []),
            "feed": (e.get("feed") or {}).get("result"),
            "mi": (e.get("mi") or {}).get("result"),
            "exit": cycle_exit(e["name"]),
        })
    name = current["name"] if current else (recent[0]["name"] if recent else None)
    return {
        "installed": True,
        "dir": str(HARNESS_DIR),
        "loopPids": pids.split(),
        "paused": (HARNESS_DIR / "stop.fleet").exists(),
        "current": current,
        "recent": recent,
        "openFindings": open_findings(),
        "cycleLog": tail(HARNESS_DIR / f"{name}.cycle.log") if name else [],
        "harnessLog": tail(HARNESS_DIR / "harness.log", 10),
    }


def cycle_exit(name):
    for line in reversed(tail(HARNESS_DIR / f"{name}.cycle.log", 400)):
        if line.startswith("CYCLE_EXIT "):
            return int(line.split()[1])
    return None


def verdict(s):
    red, amber = [], []
    sysd, stack, h = s["system"], s["containers"], s["harness"]
    if stack is None:
        red.append("docker is not answering")
    else:
        down = [n for n, c in stack.items() if n in STACK and c["state"] != "running"]
        if down:
            red.append("not running: " + ", ".join(down))
        for n, c in stack.items():
            if c.get("oomKilled"):
                red.append(f"{n} was OOM-killed")
            if c.get("health") == "unhealthy":
                red.append(f"{n} is unhealthy")
            if c.get("restarts"):
                amber.append(f"{n} restarted {c['restarts']}x")
    if s["opsHealth"] != 200:
        red.append(f"ops-api /health answered {s['opsHealth']}")
    if not h["installed"]:
        red.append(f"no harness at {h['dir']}")
    else:
        if h["paused"]:
            amber.append("the harness is paused (stop.fleet exists)")
        elif not h["loopPids"]:
            red.append("the fleet loop is not running")
        if h["current"] and h["current"]["overdue"]:
            red.append(f"{h['current']['name']} is past its {h['current']['seconds']}s budget")
        if h["recent"] and h["recent"][0]["exit"] not in (0, None):
            amber.append(f"{h['recent'][0]['name']} exited {h['recent'][0]['exit']}")
    disk = sysd["diskUsed"] / sysd["diskTotal"]
    if disk > 0.9:
        red.append(f"disk {disk:.0%} full")
    elif disk > 0.8:
        amber.append(f"disk {disk:.0%} full")
    if sysd["memAvailable"] < 0.05 * sysd["memTotal"]:
        red.append("less than 5% memory available")
    if sysd["swapTotal"] and sysd["swapUsed"] > 0.5 * sysd["swapTotal"]:
        amber.append("more than half of swap in use")
    if sysd["load"][1] > 2 * sysd["cores"]:
        amber.append(f"5-minute load {sysd['load'][1]} on {sysd['cores']} cores")
    if sysd["tempC"] and sysd["tempC"] > 85:
        amber.append(f"CPU at {sysd['tempC']:.0f}°C")
    return ("red" if red else "amber" if amber else "green"), red + amber


_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()


def status():
    with _lock:
        if time.time() - _cache["at"] > CACHE_SECONDS:
            s = {"host": os.uname().nodename,
                 "checkedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "system": system(), "containers": containers(),
                 "opsHealth": ops_health(), "harness": harness()}
            s["verdict"], s["problems"] = verdict(s)
            _cache.update(at=time.time(), data=s)
        return _cache["data"]


def gib(n):
    return f"{n / 2**30:.1f} GiB"


def duration(sec):
    if sec is None:
        return "–"
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def esc(x):
    return html.escape("" if x is None else str(x))


def page(s):
    sysd, stack, h = s["system"], s["containers"], s["harness"]
    problems = "".join(f"<li>{esc(p)}</li>" for p in s["problems"]) or "<li>nothing to report</li>"
    rows = ""
    for n, c in (stack or {}).items():
        bad = c["state"] != "running" or c.get("oomKilled") or c.get("health") == "unhealthy"
        rows += (f"<tr class='{'bad' if bad else ''}'><td>{esc(n)}</td><td>{esc(c['state'])}</td>"
                 f"<td>{esc(c.get('health') or '')}</td><td>{esc(c.get('restarts', ''))}</td>"
                 f"<td>{'yes' if c.get('oomKilled') else ''}</td><td>{esc(c.get('mem', ''))}</td>"
                 f"<td>{esc(c.get('memPerc', ''))}</td><td>{esc(c.get('cpu', ''))}</td>"
                 f"<td>{esc(c.get('startedAt', ''))}</td></tr>")
    if h["installed"]:
        cur = h["current"]
        cur_html = (f"<b>{esc(cur['name'])}</b> started {esc(cur['started'])}, "
                    f"{duration(cur['elapsed'])} of {duration(cur['seconds'])}"
                    + (" <span class='badtext'>overdue</span>" if cur["overdue"] else "")
                    if cur else "no cycle in progress")
        loop = ("paused" if h["paused"] else
                f"running (pids {' '.join(h['loopPids'])})" if h["loopPids"] else "not running")
        cycles = "".join(
            f"<tr class='{'bad' if r['exit'] not in (0, None) else ''}'><td>{esc(r['name'])}</td>"
            f"<td>{esc(r['at'])}</td><td>{duration(r['took'])}</td><td>{esc(r['exit'])}</td>"
            f"<td>{r['trials']}</td><td>{r['violations']}</td><td>{esc(r['serviceErrors'])}</td>"
            f"<td>{esc(r['feed'])}</td><td>{esc(r['mi'])}</td>"
            f"<td>{esc('; '.join(r['newRules']))}</td></tr>" for r in h["recent"])
        harness_html = f"""
<p>Loop: <b>{esc(loop)}</b> · Current cycle: {cur_html} · Open findings: <b>{esc(h['openFindings'])}</b></p>
<table><tr><th>cycle</th><th>ended</th><th>took</th><th>exit</th><th>trials</th><th>violations</th>
<th>service errors</th><th>feed</th><th>MI</th><th>new rules</th></tr>{cycles}</table>
<h3>Cycle log (last 15 lines)</h3><pre>{esc(chr(10).join(h['cycleLog']))}</pre>
<h3>harness.log (last 10 lines)</h3><pre>{esc(chr(10).join(h['harnessLog']))}</pre>"""
    else:
        harness_html = f"<p>No harness directory at <code>{esc(h['dir'])}</code>.</p>"
    temp = f"{sysd['tempC']:.0f}°C" if sysd["tempC"] else "–"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30"><title>Harness health</title>
<style>
:root {{ --bg:#f4f7f6; --fg:#16211f; --muted:#5b6b67; --line:#d5dfdc; --accent:#0f5c52;
        --green:#1d7a4f; --amber:#b7791f; --red:#b42318; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#101816; --fg:#e3ece9; --muted:#8fa39e;
        --line:#26332f; --accent:#4fb3a3; }} }}
body {{ margin:0; padding:16px; background:var(--bg); color:var(--fg);
        font:16px/1.45 system-ui, sans-serif; }}
h1 {{ margin:0 0 4px; font-size:1.4rem; }} h2 {{ margin:24px 0 8px; color:var(--accent); font-size:1.1rem; }}
h3 {{ margin:16px 0 4px; font-size:.95rem; color:var(--muted); }}
.banner {{ padding:10px 14px; border-radius:6px; color:#fff; font-weight:600; }}
.green {{ background:var(--green); }} .amber {{ background:var(--amber); }} .red {{ background:var(--red); }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:8px; }}
.tile {{ border:1px solid var(--line); border-radius:6px; padding:8px 10px; }}
.tile span {{ display:block; color:var(--muted); font-size:.8rem; }}
.scroll {{ overflow-x:auto; }} table {{ border-collapse:collapse; width:100%; font-size:.9rem; }}
th, td {{ text-align:left; padding:4px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }}
tr.bad td, .badtext {{ color:var(--red); font-weight:600; }}
pre {{ background:rgba(127,127,127,.1); padding:8px; overflow-x:auto; font-size:.8rem; }}
.muted {{ color:var(--muted); font-size:.85rem; }}
</style></head><body>
<h1>{esc(s['host'])}</h1><p class="muted">checked {esc(s['checkedAt'])} · refreshes every 30s · <a href="/api/status">JSON</a></p>
<div class="banner {s['verdict']}">{s['verdict'].upper()}</div><ul>{problems}</ul>
<h2>Host</h2><div class="grid">
<div class="tile"><span>load 1/5/15 ({sysd['cores']} cores)</span>{' / '.join(map(str, sysd['load']))}</div>
<div class="tile"><span>memory available</span>{gib(sysd['memAvailable'])} of {gib(sysd['memTotal'])}</div>
<div class="tile"><span>swap used</span>{gib(sysd['swapUsed'])} of {gib(sysd['swapTotal'])}</div>
<div class="tile"><span>disk used</span>{gib(sysd['diskUsed'])} of {gib(sysd['diskTotal'])}</div>
<div class="tile"><span>CPU temperature</span>{temp}</div>
<div class="tile"><span>uptime</span>{duration(sysd['uptimeSeconds'])}</div>
<div class="tile"><span>ops-api /health</span>{esc(s['opsHealth'] or 'no answer')}</div>
</div>
<h2>Stack</h2><div class="scroll"><table><tr><th>service</th><th>state</th><th>health</th><th>restarts</th>
<th>OOM</th><th>memory</th><th>of limit</th><th>CPU</th><th>started</th></tr>{rows or '<tr><td colspan=9>docker is not answering</td></tr>'}</table></div>
<h2>Harness</h2><div class="scroll">{harness_html}</div>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            body, kind = json.dumps(status(), indent=2).encode(), "application/json"
        elif self.path in ("/", "/index.html"):
            body, kind = page(status()).encode(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
