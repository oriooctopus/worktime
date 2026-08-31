#!/usr/bin/env python3
"""Web view of the tracker: current dot state plus today's timeline.

Reuses indicator-dot.py's report()/parse() so the page, the `dot` command and
the /indicator-dot skill can never disagree about what the status is.

Binds 0.0.0.0 so it is reachable over the tailnet; there is no auth because it
exposes only calendar row labels already visible in the vault.
"""
import importlib.util, json, os, sys
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
footer{color:var(--dim);font-size:12px;margin-top:30px}
</style></head><body><main>
<div class="status"><div class="dot" id="dot"></div><h1 id="head">…</h1></div>
<div class="sub" id="sub"></div>
<h2>Today</h2><div class="tl" id="tl"></div>
<div class="ticks"><span>6am</span><span>9am</span><span>12pm</span><span>3pm</span><span>6pm</span><span>9pm</span></div>
<div class="key"><span class="m">Meeting</span><span class="b">Browsing</span><span class="p">Personal</span></div>
<h2>Stretches</h2><table id="rows"></table>
<footer id="foot"></footer></main><script>
const S=6*60,E=22*60, mins=t=>{const[a,b]=t.split(":");return a*60|0,+a*60+ +b};
function kind(r){return r[3]!=="work"?"personal":r[2].includes("browsing")?"browsing":"meeting"}
async function tick(){
  let d; try{d=await(await fetch("/api/status",{cache:"no-store"})).json()}catch(e){return}
  const st=d.state;
  dot.className="dot "+(st==="green"?"green":st==="amber"?"amber":"red");
  head.textContent=d.headline;
  sub.innerHTML=d.detail.map(x=>`<div>${x}</div>`).join("");
  tl.innerHTML="";
  for(const r of d.rows){
    const a=Math.max(mins(r[0]),S), b=Math.min(mins(r[1]),E); if(b<=a)continue;
    const el=document.createElement("i"); el.className=kind(r);
    el.style.left=((a-S)/(E-S)*100)+"%"; el.style.width=((b-a)/(E-S)*100)+"%";
    el.title=`${r[0]}–${r[1]} ${r[2]}`; tl.appendChild(el);
  }
  const n=document.createElement("div"); n.className="now";
  n.style.left=((mins(d.now)-S)/(E-S)*100)+"%"; tl.appendChild(n);
  rows.innerHTML=d.rows.map(r=>`<tr><td class="t">${r[0]}–${r[1]}</td><td>${r[2]}</td>
    <td class="m">${mins(r[1])-mins(r[0])}m</td></tr>`).join("");
  foot.textContent=`${d.generated?"generated "+d.generated:""} · refreshed ${d.now}`;
}
tick(); setInterval(tick,15000);
</script></body></html>"""


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
    return {"state": {0: "green", 1: "amber"}.get(code, "red"),
            "headline": lines[0].split(" - ", 1)[-1] if " - " in lines[0] else lines[0],
            "detail": [l.strip() for l in lines[1:]], "rows": rows,
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
