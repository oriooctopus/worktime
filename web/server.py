#!/usr/bin/env python3
"""Web view of the tracker: current dot state plus today's timeline.

Reuses indicator-dot.py's report()/parse() so the page, the `dot` command and
the /indicator-dot skill can never disagree about what the status is.

Binds 0.0.0.0 so it is reachable over the tailnet; there is no auth because it
exposes only calendar row labels already visible in the vault.
"""
import importlib.util, json, os, re, sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "indicator_dot", os.path.join(ROOT, "skills", "indicator-dot", "indicator-dot.py"))
ind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ind)

PORT = int(os.environ.get("WORKTIME_WEB_PORT", "8313"))

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>worktime</title><style>
:root{--bg:#0f1115;--fg:#e6e8eb;--dim:#8b93a1;--line:#232833;
      --green:#2ea043;--amber:#d29922;--red:#da3633}
@media(prefers-color-scheme:light){:root{--bg:#fff;--fg:#1b1f24;--dim:#656d76;--line:#d8dee4}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
     display:flex;justify-content:center;padding:6vh 16px}
main{width:100%;max-width:720px}
.status{display:flex;align-items:center;gap:16px;margin-bottom:6px}
.dot{width:34px;height:34px;border-radius:50%;flex:0 0 auto}
.dot.green{background:var(--green);box-shadow:0 0 0 6px color-mix(in srgb,var(--green) 18%,transparent)}
.dot.amber{background:var(--amber);box-shadow:0 0 0 6px color-mix(in srgb,var(--amber) 18%,transparent)}
.dot.red{background:var(--red);box-shadow:0 0 0 6px color-mix(in srgb,var(--red) 18%,transparent)}
h1{font-size:22px;margin:0;font-weight:600}
.sub{color:var(--dim);font-size:14px;margin:0 0 26px 50px}
.sub div{margin-top:2px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);
   font-weight:600;margin:26px 0 10px}
.tl{position:relative;height:38px;background:var(--line);border-radius:5px;overflow:hidden}
.tl i{position:absolute;top:0;bottom:0;opacity:.9}
.tl i.meeting{background:var(--green)}
.tl i.browsing{background:#3b82f6}
.tl i.personal{background:var(--dim);opacity:.45}
.tl .now{position:absolute;top:-4px;bottom:-4px;width:2px;background:var(--fg)}
.ticks{display:flex;justify-content:space-between;color:var(--dim);font-size:11px;margin-top:5px}
table{width:100%;border-collapse:collapse;font-size:14px}
td{padding:5px 0;border-bottom:1px solid var(--line)}
td.t{color:var(--dim);width:110px;font-variant-numeric:tabular-nums}
td.m{text-align:right;color:var(--dim);width:64px}
.key{display:flex;gap:16px;color:var(--dim);font-size:12px;margin-top:9px}
.key span::before{content:"";display:inline-block;width:9px;height:9px;border-radius:2px;
                  margin-right:5px;vertical-align:-1px}
.key .m::before{background:var(--green)}.key .b::before{background:#3b82f6}
.key .p::before{background:var(--dim);opacity:.45}
#sess td.big{font-variant-numeric:tabular-nums}
#sess .parts{color:var(--dim);font-size:12.5px;margin-top:2px}
#sess .parts b{color:var(--fg);font-weight:500}
tr.on td{color:var(--green)}
tr.on.personal td{color:var(--dim)}
tr.on.personal em{color:var(--dim);border-color:var(--dim)}
em{font-style:normal;font-size:11px;text-transform:uppercase;letter-spacing:.06em;
   color:var(--green);border:1px solid var(--green);border-radius:3px;padding:0 4px;margin-left:6px}
footer{color:var(--dim);font-size:12px;margin-top:30px}
</style></head><body><main>
<div class="status"><div class="dot" id="dot"></div><h1 id="head">…</h1></div>
<div class="sub" id="sub"></div>
<h2>Today</h2><div class="tl" id="tl"></div>
<div class="ticks"><span>6am</span><span>9am</span><span>12pm</span><span>3pm</span><span>6pm</span><span>9pm</span></div>
<div class="key"><span class="m">Meeting</span><span class="b">Browsing</span><span class="p">Personal</span></div>
<div id="sesswrap" hidden><h2>Sessions</h2><table id="sess"></table></div>
<h2 id="strh">Stretches</h2><table id="rows"></table>
<footer id="foot"></footer></main><script>
const S=6*60,E=22*60, mins=t=>{const[a,b]=t.split(":");return a*60|0,+a*60+ +b};
function kind(r){return r.source!=="work"?"personal":r.label.includes("browsing")?"browsing":"meeting"}
async function tick(){
  let d; try{d=await(await fetch("/api/status",{cache:"no-store"})).json()}catch(e){return}
  const st=d.state;
  dot.className="dot "+(st==="green"?"green":st==="amber"?"amber":"red");
  head.textContent=d.headline;
  sub.innerHTML=d.detail.map(x=>`<div>${x}</div>`).join("");
  tl.innerHTML="";
  for(const r of d.rows){
    const a=Math.max(mins(r.start),S), b=Math.min(mins(r.shown_end),E); if(b<=a)continue;
    const el=document.createElement("i"); el.className=kind(r);
    el.style.left=((a-S)/(E-S)*100)+"%"; el.style.width=(Math.max(b-a,1)/(E-S)*100)+"%";
    el.title=`${r.start}–${r.shown_end} ${r.label}`; tl.appendChild(el);
  }
  const n=document.createElement("div"); n.className="now";
  n.style.left=((mins(d.now)-S)/(E-S)*100)+"%"; tl.appendChild(n);
  if(d.periods){
    sesswrap.hidden=false; strh.textContent="Stretches (raw)";
    sess.innerHTML=d.periods.map(p=>{
      const bits=[];
      if(p.n_prompts) bits.push(`<b>${p.n_prompts}</b> prompts`);
      if(p.n_slack)   bits.push(`<b>${p.n_slack}</b> Slack`);
      if(p.meeting)   bits.push(`meeting ${p.meeting}`);
      if(p.marked)    bits.push("marked");
      const h=Math.floor(p.minutes/60), m=p.minutes%60;
      return `<tr><td class="t big">${p.start}–${p.end}</td>
        <td>${p.what||"—"}<div class="parts">${bits.join(" · ")||"&nbsp;"}</div></td>
        <td class="m">${h?h+"h":""}${h?String(m).padStart(2,"0"):m+"m"}</td></tr>`;
    }).join("");
  } else { sesswrap.hidden=true; strh.textContent="Stretches"; }
  rows.innerHTML=d.rows.length? d.rows.map(r=>`<tr${r.ongoing?' class="on'+(r.source!=="work"?" personal":"")+'"':""}>
    <td class="t">${r.start}–${r.shown_end}</td>
    <td>${r.label}${r.ongoing?' <em>now</em>':""}</td>
    <td class="m">${mins(r.shown_end)-mins(r.start)}m</td></tr>`).join("")
    : `<tr><td class="t">—</td><td>nothing tracked yet today</td><td class="m"></td></tr>`;
  foot.textContent=`${d.generated?"generated "+d.generated:""} · refreshed ${d.now}`;
}
tick(); setInterval(tick,15000);
</script></body></html>"""


SNAPSHOT_ROW = re.compile(
    r"^\|\s*(\d{2}:\d{2})\s*\|\s*(\d{2}:\d{2})\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|"
    r"\s*(\d+)\s*\|\s*(\w*)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*$")


def snapshot_path(now):
    """Today's markdown sidecar, written by the probe on the machine it runs on."""
    return os.path.join(ind.DASHBOARD, "worktime", now.strftime("%Y-%m-%d") + ".md")


def parse_snapshot(text):
    """Work periods from the probe's markdown sidecar.

    These are the probe's own conclusions -- prompts, meetings and marks already
    unioned into sessions by merge_spans. Nothing here recomputes that: a second
    definition of a session would drift from the one the dot actually uses.
    """
    periods = []
    for line in text.splitlines():
        m = SNAPSHOT_ROW.match(line)
        if not m or m.group(1) == "Start":
            continue
        start, end, mins, prompts, slack, marked, meeting, what = m.groups()
        periods.append({"start": start, "end": end, "minutes": int(mins),
                        "n_prompts": int(prompts), "n_slack": int(slack),
                        "marked": bool(marked.strip()),
                        "meeting": meeting.strip(), "what": what.strip()})
    periods.sort(key=lambda p: minutes(p["start"]), reverse=True)
    return periods


def visible_rows(rows, now_min):
    """Rows that have actually happened, most recent first.

    A row starting later today is a plan, not a record -- showing it invites the
    same mistake as a mark that claims time before it is earned. A row in
    progress is clipped to now for the same reason: a 16:30-17:30 meeting at
    17:00 has produced 30 minutes, not 60.
    """
    out = []
    for start, end, label, source in rows:
        if minutes(start) > now_min:
            continue
        ongoing = minutes(end) > now_min
        out.append({"start": start, "end": end, "label": label, "source": source,
                    "ongoing": ongoing,
                    "shown_end": f"{now_min // 60:02d}:{now_min % 60:02d}" if ongoing else end})
    out.sort(key=lambda r: (minutes(r["start"]), minutes(r["end"])), reverse=True)
    return out


def minutes(hhmm):
    return ind.minutes(hhmm)


def load_periods(now):
    path = snapshot_path(now)
    if not os.path.exists(path):
        return None  # None means "no probe data here", not "no work today"
    try:
        return parse_snapshot(open(path).read())
    except OSError:
        return None


def status():
    path = ind.CALENDAR
    if not os.path.exists(path):
        return {"state": "red", "headline": "No calendar data",
                "detail": [f"nothing at {path}"], "rows": [], "generated": None,
                "now": datetime.now().strftime("%H:%M")}
    text = open(path).read()
    now = datetime.now(ind.ZoneInfo("America/New_York"))
    age = (datetime.now().timestamp() - os.path.getmtime(path)) / 3600
    code, lines = ind.report(text, now, age)
    rows, generated = ind.parse(text)
    now_min = now.hour * 60 + now.minute
    return {"state": {0: "green", 1: "amber"}.get(code, "red"),
            "headline": lines[0].split(" - ", 1)[-1] if " - " in lines[0] else lines[0],
            "detail": [l.strip() for l in lines[1:]],
            "rows": visible_rows(rows, now_min),
            "periods": load_periods(now),
            "generated": generated, "now": now.strftime("%H:%M")}


class H(BaseHTTPRequestHandler):
    def _send(self, body, ctype):
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/api/status"):
            self._send(json.dumps(status()), "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(PAGE, "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
