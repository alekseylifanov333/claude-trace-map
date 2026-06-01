#!/usr/bin/env python3
"""
trace_app.py — the /trace_map app.
Opens a NATIVE window listing the last N (default 10) real user queries across
recent Claude Code sessions. Click any one to build its 3D "star-field" trace
map (route + memory/skill stars + tokens/time/errors + plain-language story).

Reuses the parser from trace_map.py. Does not modify it.

Usage:
    python3 trace_app.py [N]
"""
import sys, os, json, tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
OUTDIR = Path(tempfile.gettempdir()) / "trace-map"; OUTDIR.mkdir(exist_ok=True)
import webview
from trace_map import (load, blocks, role, usage, text_of, is_user_turn, trim,
                       context_signals, extract_decision)

HANDOFF = str(OUTDIR / "handoff.json")
_WINDOW = None

class Api:
    """JS->Python bridge. On submit, writes the handoff file and closes the
    window so control returns to Claude, which then reads handoff.json."""
    def submit(self, payload_json):
        import threading
        data = json.loads(payload_json)
        Path(HANDOFF).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        # destroy AFTER this call returns — closing the window inside the JS-API
        # callback deadlocks Cocoa (JS awaits the method while it tears down the webview).
        threading.Timer(0.15, lambda: _WINDOW.destroy() if _WINDOW is not None else None).start()
        return "ok"

    def latest(self):
        """For Live mode: parse the most recent transcript's last exchange."""
        base = Path.home() / ".claude" / "projects"
        files = sorted(base.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files: return "null"
        f = files[0]; mt = context_signals(str(f))[0]
        sd = Path.home() / ".claude" / "skills"
        sn = sorted(d.name for d in sd.iterdir() if d.is_dir()) if sd.exists() else []
        exs = exchanges_of(f, mt)
        if not exs: return "null"
        e = exs[-1]; e["memory_titles"] = mt; e["skill_names"] = sn; e["session"] = f.stem[:8] + " · LIVE"
        return json.dumps(e, ensure_ascii=False)

# ---------- extract every exchange from a transcript ----------
def build_exchange(rows, start, end, mem_titles):
    query = text_of(rows[start])
    thinking, answer, actions = [], [], []
    id2idx, recalled = {}, set()
    total_out = total_in = total_ms = 0
    total_cache = total_in_sum = 0
    model = ""; grp = []
    for o in rows[start+1:end]:
        t = o.get("type")
        if t == "assistant":
            u = usage(o); out = u.get("output_tokens", 0) or 0
            cr = u.get("cache_read_input_tokens", 0) or 0; it = u.get("input_tokens", 0) or 0
            inn = it + cr
            total_out += out; total_in = max(total_in, inn)
            total_cache += cr; total_in_sum += it
            m = o.get("message")
            if isinstance(m, dict) and m.get("model") and m["model"] != "<synthetic>": model = m["model"]
            tu = [b for b in blocks(o) if isinstance(b, dict) and b.get("type") == "tool_use"]
            per = round(out / len(tu)) if tu else 0; grp = []
            for b in blocks(o):
                if not isinstance(b, dict): continue
                bt = b.get("type")
                if bt == "thinking": thinking.append(b.get("thinking") or b.get("text") or "")
                elif bt == "text" and b.get("text", "").strip(): answer.append(b["text"])
                elif bt == "tool_use":
                    name, inp = b.get("name", "?"), b.get("input", {})
                    if name == "Skill": label = f"Skill: {inp.get('skill','?')}"
                    elif name == "Bash": label = inp.get("description") or trim(inp.get("command", ""), 60)
                    elif name in ("Read", "Edit", "Write"): label = os.path.basename(str(inp.get("file_path", "")))
                    else: label = name
                    fp = str(inp.get("file_path", ""))
                    a = {"tool": name, "label": label,
                         "is_skill": name == "Skill" or (name == "Read" and fp.endswith("SKILL.md")),
                         "detail": trim(json.dumps(inp, ensure_ascii=False), 500),
                         "tokens_out": per, "ms": None, "error": False, "error_msg": ""}
                    id2idx[b.get("id")] = len(actions); grp.append(len(actions)); actions.append(a)
        elif t == "system" and o.get("subtype") == "turn_duration":
            d = o.get("durationMs", 0) or 0; total_ms += d
            for gi in grp: actions[gi]["ms"] = d
        elif t == "user":
            for b in blocks(o):
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                    idx = id2idx.get(b.get("tool_use_id"))
                    if idx is not None:
                        actions[idx]["error"] = True
                        c = b.get("content")
                        actions[idx]["error_msg"] = trim(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False), 200)
            txt = text_of(o)
            if "system-reminder" in txt:
                for title in mem_titles:
                    if title[:24].lower() in txt.lower(): recalled.add(title)
    used = next((a["label"].split(": ", 1)[-1] for a in actions if a["is_skill"]), None)
    # diagnosis heuristics — what looks off about this run
    diag = []
    errs = [a for a in actions if a["error"]]
    if errs: diag.append(f"{len(errs)} step(s) failed with an error")
    nbash = len([a for a in actions if a["tool"] == "Bash"])
    if nbash >= 6: diag.append(f"{nbash} bash calls — possibly repeats/extra steps")
    if total_in_sum > 60000: diag.append(f"large un-cached input (~{total_in_sum//1000}k tokens)")
    if actions and not "".join(answer).strip(): diag.append("actions taken but no final answer")
    return {"query": trim(query, 800), "thinking": trim("\n\n".join(thinking), 1400),
            "decision": extract_decision("\n".join(thinking)), "actions": actions,
            "answer": trim("\n\n".join(answer), 1200), "memory_recalled": sorted(recalled),
            "total_out": total_out, "total_in": total_in, "total_ms": total_ms,
            "cache": total_cache, "in_sum": total_in_sum, "diagnosis": diag,
            "model": model, "skill_used": used}

def exchanges_of(path, mem_titles):
    rows = load(path)
    uturns = [i for i, o in enumerate(rows) if is_user_turn(o)]
    out = []
    for k, s in enumerate(uturns):
        e = build_exchange(rows, s, uturns[k+1] if k+1 < len(uturns) else len(rows), mem_titles)
        e["ts"] = rows[s].get("timestamp", "")
        out.append(e)
    return out

def recent_traces(n=10, scan_files=14):
    base = Path.home() / ".claude" / "projects"
    files = sorted(base.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:scan_files]
    sd = Path.home() / ".claude" / "skills"
    skill_names = sorted(d.name for d in sd.iterdir() if d.is_dir()) if sd.exists() else []
    proj_mem = {}
    all_ex = []
    for f in files:
        proj = f.parent.name
        if proj not in proj_mem:
            mt, _ = context_signals(str(f)); proj_mem[proj] = mt
        for e in exchanges_of(f, proj_mem[proj]):
            q = e["query"].strip()
            if not q or q.startswith("<task-notification") or q.startswith("[SYSTEM NOTIFICATION"): continue
            e["project"] = proj.replace("-Users-alexlifanov-", "…/")
            e["session"] = f.stem[:8]
            e["memory_titles"] = proj_mem[proj]
            e["skill_names"] = skill_names
            all_ex.append(e)
    all_ex.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return all_ex[:n]

# ---------- overview of memory + skills (the important bits) ----------
def _frontmatter(text):
    if text.startswith("---"):
        e = text.find("\n---", 3)
        return text[3:e] if e > 0 else text[3:]
    return ""

def build_overview(top_mem=18, top_skill=12, scan_files=8):
    import collections, re as _re
    base = Path.home() / ".claude" / "projects"
    files = sorted(base.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:scan_files]
    proj = files[0].parent.name if files else ""
    memdir = Path.home() / ".claude" / "projects" / proj / "memory"
    entries, name_of_file, type_of, texts = [], {}, {}, {}
    md = memdir / "MEMORY.md"
    if md.exists():
        for m in _re.finditer(r"^\s*- \[([^\]]+)\]\(([^)]+)\)\s*[—-]+\s*(.*)$", md.read_text(encoding="utf-8", errors="ignore"), _re.M):
            entries.append({"name": m.group(1), "file": m.group(2), "desc": trim(m.group(3), 160)})
    if memdir.exists():
        for f in memdir.glob("*.md"):
            if f.name == "MEMORY.md": continue
            t = f.read_text(encoding="utf-8", errors="ignore"); fm = _frontmatter(t)
            nm = _re.search(r"name:\s*(.+)", fm); ty = _re.search(r"type:\s*([a-z]+)", fm)
            slug = nm.group(1).strip() if nm else f.stem
            name_of_file[f.name] = slug; type_of[slug] = (ty.group(1) if ty else ""); texts[slug] = t
    links = collections.Counter()
    for t in texts.values():
        for s in _re.findall(r"\[\[([a-z0-9-]+)\]\]", t): links[s] += 1
    tw = {"user": 4, "feedback": 3, "project": 2, "reference": 1}
    for e in entries:
        slug = name_of_file.get(e["file"], "")
        e["type"] = type_of.get(slug, ""); e["links"] = links.get(slug, 0)
        e["score"] = tw.get(e["type"], 1) + e["links"]
    mem = sorted(entries, key=lambda e: e["score"], reverse=True)[:top_mem]
    sd = Path.home() / ".claude" / "skills"; sk = []
    if sd.exists():
        raw = "".join(f.read_text(encoding="utf-8", errors="ignore") for f in files)
        for d in sorted(p for p in sd.iterdir() if p.is_dir()):
            desc = ""; sm = d / "SKILL.md"
            if sm.exists():
                dm = _re.search(r"description:\s*(.+)", _frontmatter(sm.read_text(encoding="utf-8", errors="ignore")))
                desc = trim(dm.group(1), 220) if dm else ""
            sk.append({"name": d.name, "desc": desc, "score": raw.count(d.name)})
    skills = sorted(sk, key=lambda s: s["score"], reverse=True)[:top_skill]
    return {"project": proj.replace("-Users-alexlifanov-", "…/"), "mem_total": len(entries),
            "skill_total": len(sk), "memory": mem, "skills": skills}

# ---------- spend dashboard (tokens / time / $ across sessions) ----------
def _cost(in_sum, cache, out):
    return in_sum / 1e6 * 15 + cache / 1e6 * 1.5 + out / 1e6 * 75

def build_spend(scan_files=16, top=8):
    base = Path.home() / ".claude" / "projects"
    files = sorted(base.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:scan_files]
    proj_mem, rows = {}, []
    for f in files:
        proj = f.parent.name
        if proj not in proj_mem: proj_mem[proj] = context_signals(str(f))[0]
        for e in exchanges_of(f, proj_mem[proj]):
            q = e["query"].strip()
            if not q or q.startswith("<task-notification") or q.startswith("[SYSTEM NOTIFICATION"): continue
            rows.append({"query": q[:90], "cost": _cost(e["in_sum"], e["cache"], e["total_out"]),
                         "in": e["in_sum"], "cache": e["cache"], "out": e["total_out"],
                         "ms": e["total_ms"], "skill": e["skill_used"], "ts": e.get("ts", "")})
    tin = sum(r["in"] for r in rows); tcache = sum(r["cache"] for r in rows)
    tout = sum(r["out"] for r in rows); tms = sum(r["ms"] for r in rows)
    per = {}
    for r in rows:
        k = r["skill"] or "(no skill)"
        d = per.setdefault(k, {"skill": k, "count": 0, "cost": 0.0, "out": 0})
        d["count"] += 1; d["cost"] += r["cost"]; d["out"] += r["out"]
    return {"queries": len(rows), "files": len(files), "in": tin, "cache": tcache, "out": tout,
            "ms": tms, "cost": sum(r["cost"] for r in rows),
            "cache_rate": (tcache / (tcache + tin)) if (tcache + tin) else 0,
            "per_skill": sorted(per.values(), key=lambda d: d["cost"], reverse=True),
            "top": sorted(rows, key=lambda r: r["cost"], reverse=True)[:top]}

# ============================ APP HTML ============================
APP_HTML = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Trace Map</title>
<style>
 html,body{margin:0;height:100%;background:#05070d;color:#e6edf3;font:14px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;overflow:hidden}
 /* menu */
 #menu{position:fixed;inset:0;z-index:60;background:#05070d;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px;box-sizing:border-box}
 #menu h1{font-size:26px;margin:0 0 4px}#menu .sub{color:#7d8590;font-size:13px;margin-bottom:24px}
 #menu .cards{display:flex;gap:20px;flex-wrap:wrap;justify-content:center}
 .card{width:300px;padding:26px;border:1px solid #182030;border-radius:16px;background:#0a0f18;cursor:pointer;transition:.12s}
 .card:hover{border-color:#2b4a6e;background:#0e1726;transform:translateY(-3px)}
 .card .ic{font-size:36px}.card .t{font-size:17px;margin:12px 0 6px}.card .d{font-size:13px;color:#7d8590;line-height:1.5}
 .mlink{position:fixed;left:16px;top:14px;z-index:55;color:#7d8590;cursor:pointer;font-size:13px}
 /* picker */
 #picker{position:fixed;inset:0;z-index:50;background:#05070d;overflow:auto;padding:34px 40px;box-sizing:border-box;display:none}
 /* add form */
 #addscreen{position:fixed;inset:0;z-index:55;background:#05070d;overflow:auto;padding:34px 40px;box-sizing:border-box;display:none}
 #addscreen h1{font-size:21px;margin:0 0 18px}.formwrap{max-width:560px}
 .seg{display:flex;gap:8px;margin-bottom:20px}
 .seg button{flex:1;padding:13px;border-radius:10px;background:#0a0f18;border:1px solid #182030;color:#9aa7b5;cursor:pointer;font-size:14px}
 .seg button.on{background:#15233a;border-color:#2b4a6e;color:#e6edf3}
 .fld{margin-bottom:16px}.fld label{display:block;font-size:12px;color:#7d8590;margin-bottom:6px}
 .fld input,.fld textarea{width:100%;box-sizing:border-box;background:#0a0f18;border:1px solid #182030;border-radius:9px;color:#e6edf3;padding:11px;font:14px/1.5 -apple-system,Segoe UI,sans-serif}
 .fld textarea{min-height:120px;resize:vertical}
 .addbtn{padding:12px 24px;border-radius:10px;background:#10331c;border:1px solid #2ea043;color:#7ee787;cursor:pointer;font-size:15px;font-weight:600}
 .aerr{color:#ff7b72;font-size:12px;margin-top:10px;display:none}
 /* spend */
 #spendscreen{position:fixed;inset:0;z-index:55;background:#05070d;overflow:auto;padding:34px 40px;box-sizing:border-box;display:none}
 #spendscreen h1{font-size:21px;margin:0 0 18px}
 .kpis{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:8px}
 .kpi{flex:1;min-width:140px;background:#0a0f18;border:1px solid #182030;border-radius:12px;padding:16px}
 .kpi .v{font-size:24px;font-weight:700;color:#e6edf3}.kpi .l{font-size:12px;color:#7d8590;margin-top:4px}
 .barrow{display:flex;align-items:center;gap:10px;margin-bottom:7px;font-size:13px}
 .barrow .nm{width:230px;color:#cdd6e0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .bar{flex:1;height:14px;background:#0e1726;border-radius:7px;overflow:hidden}
 .bar i{display:block;height:100%;background:linear-gradient(90deg,#2ea043,#7ee787)}
 .barrow .cv{width:70px;text-align:right;color:#9aa7b5}
 .sec2{font-size:12px;color:#9aa7b5;text-transform:uppercase;letter-spacing:.5px;margin:24px 0 10px}
 #fixbar{margin-bottom:12px}
 #picker h1{font-size:20px;margin:0 0 4px}#picker .hd{color:#7d8590;font-size:13px;margin-bottom:22px}
 .row{display:flex;gap:14px;align-items:flex-start;padding:14px 16px;border:1px solid #182030;border-radius:12px;margin-bottom:10px;cursor:pointer;transition:.12s;background:#0a0f18}
 .row:hover{border-color:#2b4a6e;background:#0e1726;transform:translateX(2px)}
 .row .n{flex:0 0 30px;height:30px;border-radius:50%;background:#15233a;color:#9ad;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px}
 .row .q{font-size:15px;color:#e6edf3;margin-bottom:5px;line-height:1.4}
 .row .meta{font-size:12px;color:#56606e}
 .badge{display:inline-block;padding:2px 9px;border-radius:10px;font-size:11px;margin-right:6px}
 .b-skill{background:#10331c;color:#7ee787}.b-self{background:#1a2433;color:#8aa}.b-err{background:#3a1715;color:#ff7b72}
 /* map */
 #back{position:fixed;left:16px;top:14px;z-index:20;display:none;padding:7px 13px;border-radius:9px;background:#0e1726;border:1px solid #2b4a6e;color:#cfe;cursor:pointer;font-size:13px}
 #panel{position:fixed;top:0;right:0;width:360px;height:100vh;z-index:10;box-sizing:border-box;overflow:auto;padding:14px 18px;display:none;background:rgba(8,12,20,.92);border-left:1px solid #182030;backdrop-filter:blur(8px)}
 .tabs{display:flex;gap:6px;margin:34px 0 12px}
 .tab{flex:1;text-align:center;padding:7px;border-radius:8px;background:#0e1422;border:1px solid #182030;cursor:pointer;font-size:12px;color:#9aa7b5}
 .tab.on{background:#15233a;color:#e6edf3;border-color:#2b4a6e}
 .sum{font-size:12px;color:#9aa7b5;background:#0a1018;border:1px solid #182030;border-radius:8px;padding:9px 11px;margin-bottom:12px;line-height:1.7}.sum b{color:#e6edf3}
 #story{font-size:13.5px;line-height:1.7;margin-bottom:14px}#story p{margin:0 0 8px;padding-left:22px;position:relative}#story p:before{content:attr(data-i);position:absolute;left:0;top:1px;color:#56606e;font-size:11px}
 h2.sec{font-size:12px;margin:4px 0 6px;color:#9aa7b5;font-weight:600;text-transform:uppercase;letter-spacing:.5px}
 .step{display:flex;gap:9px;align-items:flex-start;padding:7px 8px;border-radius:8px;cursor:pointer;margin-bottom:3px;border:1px solid transparent}
 .step:hover{background:#0e1422}.step.sel{background:#0e1726;border-color:#1f3350}
 .num{flex:0 0 20px;height:20px;border-radius:50%;font-size:11px;display:flex;align-items:center;justify-content:center;color:#05070d;font-weight:700}
 .lbl{font-size:13px}.knd{font-size:10px;color:#56606e}.met{font-size:10px;color:#6b7785;margin-top:2px}.err{color:#ff7b72}
 #detail{margin-top:12px;border-top:1px solid #182030;padding-top:12px;display:none}#detail h3{font-size:13px;margin:0 0 6px}
 #detail pre{white-space:pre-wrap;word-break:break-word;background:#0a0f18;border:1px solid #182030;border-radius:6px;padding:9px;font-size:11px;color:#aeb9c4;max-height:40vh;overflow:auto}
 .src{font-size:10px;color:#d29922;margin-top:4px}
 body.simple .knd,body.simple .met,body.simple #techblk{display:none}
 .leg{position:fixed;left:16px;bottom:40px;z-index:10;font-size:11px;color:#7d8590;display:none}
 #hint{position:fixed;left:16px;bottom:14px;z-index:10;color:#56606e;font-size:11px;display:none}
 .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin:0 5px 0 12px;vertical-align:middle}
</style></head><body>
<pre id="err" style="position:fixed;bottom:0;left:0;right:0;z-index:9999;margin:0;background:#3a1715;color:#ffb3ad;font:12px ui-monospace,monospace;padding:8px;white-space:pre-wrap;max-height:45vh;overflow:auto;display:none"></pre>
<script>window.onerror=function(m,s,l,c,e){var d=document.getElementById('err');if(d){d.style.display='block';d.textContent+='JS error: '+m+'  ('+l+':'+c+')\n'+((e&&e.stack)||'')+'\n\n';}return false;};</script>
<div id="menu"><h1>🗺️ Trace Map</h1><div class="sub">What do you want to see?</div>
 <div class="cards">
  <div class="card" id="cOverview"><div class="ic">🧠</div><div class="t">Claude's Mind</div><div class="d">Memory and skills — the most important things the assistant has "in mind".</div></div>
  <div class="card" id="cRecent"><div class="ic">🛤️</div><div class="t">Recent queries</div><div class="d">Routes of recent questions: what went where, tokens, errors.</div></div>
  <div class="card" id="cAdd"><div class="ic">➕</div><div class="t">Add memory/skill</div><div class="d">Start a new memory entry or skill — Claude continues creating it in the chat.</div></div>
  <div class="card" id="cSpend"><div class="ic">💰</div><div class="t">Spend</div><div class="d">Tokens, time and $ across sessions: where it costs most, cache-hit, by skill.</div></div>
  <div class="card" id="cLive"><div class="ic">🔴</div><div class="t">Live</div><div class="d">Watch the route in real time as you talk to Claude.</div></div>
 </div></div>
<div id="addscreen"><div class="mlink" id="aBack">← menu</div>
 <div class="formwrap"><h1>➕ Add</h1>
  <div class="seg"><button id="kMem" class="on">🟡 Memory</button><button id="kSkill">🟢 Skill</button></div>
  <div class="fld"><label>Name</label><input id="aName" placeholder="short, specific title"></div>
  <div class="fld"><label>Description (briefly: what and why)</label><textarea id="aDesc" placeholder="Claude will ask follow-up questions and format it properly."></textarea></div>
  <button class="addbtn" id="aSubmit">Add →</button>
  <div class="aerr" id="aErr"></div>
 </div></div>
<div id="spendscreen"><div class="mlink" id="sBack">← menu</div>
 <h1>💰 Claude's Spend</h1>
 <div class="kpis" id="kpis"></div>
 <div class="sec2">Most expensive queries</div><div id="topq"></div>
 <div class="sec2">By skill</div><div id="perskill"></div>
</div>
<div id="picker"><div class="mlink" id="pBack">← menu</div><h1>🛤️ Recent queries</h1><div class="hd">Pick one to build its route map.</div><div id="list"></div></div>
<div id="back">← back</div>
<div class="leg" id="leg"><span class="dot" style="background:#58a6ff"></span>query<span class="dot" style="background:#bc8cff"></span>thinking<span class="dot" style="background:#7ee787"></span>skill<span class="dot" style="background:#ff7b72"></span>tool<span class="dot" style="background:#39c5cf"></span>answer<span class="dot" style="background:#d29922"></span>memory</div>
<div id="hint">drag — rotate · wheel — zoom · click a node/star — details</div>
<div id="panel">
 <div class="tabs"><div class="tab on" id="tSimple">📖 Simple</div><div class="tab" id="tTech">⚙️ Technical</div></div>
 <div class="sum" id="sum"></div><div id="fixbar"></div><div id="story"></div>
 <div id="detail"><h3 id="dttl"></h3><div id="dbody"></div></div>
 <div id="techblk"><div id="steps"></div></div>
</div>
<script src="https://unpkg.com/three@0.128.0/build/three.min.js"></script>
<script src="https://unpkg.com/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
<script>
const TRACES=__TRACES__;const OVERVIEW=__OVERVIEW__;const SPEND=__SPEND__;
const COL={query:0x58a6ff,think:0xbc8cff,skill:0x7ee787,action:0xff7b72,answer:0x39c5cf,mem:0xd29922,skillStar:0x2ea043,err:0xf85149};
const kfmt=n=>n>=1000?(n/1000).toFixed(1)+'k':String(n),tfmt=ms=>ms?(ms/1000).toFixed(1)+'s':'';

// ---------- picker ----------
const listEl=document.getElementById('list');
try{TRACES.forEach((T,i)=>{const errs=(T.actions||[]).filter(a=>a.error).length;
 const when=T.ts?new Date(T.ts).toLocaleString('en-GB',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'';
 const badge=T.skill_used?'<span class="badge b-skill">'+T.skill_used+'</span>':'<span class="badge b-self">self-answered</span>';
 const eb=errs?'<span class="badge b-err">⚠ '+errs+'</span>':'';
 const d=document.createElement('div');d.className='row';d.onclick=()=>openTrace(i);
 d.innerHTML='<div class="n">'+(i+1)+'</div><div><div class="q">'+(T.query||'').slice(0,140).replace(/</g,'&lt;')+'</div>'+
  '<div class="meta">'+badge+eb+when+' · '+T.project+' · '+kfmt(T.total_in||0)+'/'+kfmt(T.total_out||0)+' tok'+
  (T.memory_recalled&&T.memory_recalled.length?' · memory '+T.memory_recalled.length:'')+'</div></div>';
 listEl.appendChild(d);});}catch(e){var _e=document.getElementById('err');if(_e){_e.style.display='block';_e.textContent+='picker build error: '+e+'\n';}}

// ---------- 3D engine (built once) ----------
let scene,cam,rnd,ctrl,curve,pulse,route=[],meshes=[],memStars=[],skillStars=[],ovMeshes=[],pickAll=[],backTo='menu',_dn=null;
function init3D(){
 cam=new THREE.PerspectiveCamera(52,innerWidth/innerHeight,.1,600);
 rnd=new THREE.WebGLRenderer({antialias:true});rnd.setSize(innerWidth,innerHeight);rnd.setPixelRatio(devicePixelRatio);
 rnd.domElement.style.position='fixed';rnd.domElement.style.inset='0';rnd.domElement.style.zIndex='0';document.body.appendChild(rnd.domElement);
 ctrl=new THREE.OrbitControls(cam,rnd.domElement);ctrl.enableDamping=true;
 addEventListener('resize',()=>{cam.aspect=innerWidth/innerHeight;cam.updateProjectionMatrix();rnd.setSize(innerWidth,innerHeight);});
 addEventListener('pointerdown',e=>{_dn=[e.clientX,e.clientY];},true);
 addEventListener('pointerup',e=>{if(document.getElementById('picker').style.display==='none'){
   if(e.clientX>innerWidth-360){_dn=null;return;}if(!_dn)return;const m=Math.hypot(e.clientX-_dn[0],e.clientY-_dn[1]);_dn=null;if(m<6)pickAt(e.clientX,e.clientY);}},true);
 (function loop(){requestAnimationFrame(loop);if(curve&&pulse){window._t=((window._t||0)+.0025)%1;pulse.position.copy(curve.getPointAt(window._t));}
   if(ctrl)ctrl.update();if(scene)rnd.render(scene,cam);})();
}
function labelSprite(text,scale,color,bold){const c=document.createElement('canvas');c.width=512;c.height=160;const g=c.getContext('2d');
 g.fillStyle=color;g.font=(bold?'bold ':'')+'40px -apple-system,Segoe UI,sans-serif';g.textAlign='center';g.textBaseline='middle';
 const L=String(text).match(/.{1,22}(\s|$)/g)||[String(text)];L.slice(0,3).forEach((ln,k,a)=>g.fillText(ln.trim(),256,80+(k-(a.length-1)/2)*46));
 const sp=new THREE.Sprite(new THREE.SpriteMaterial({map:new THREE.CanvasTexture(c),transparent:true,depthWrite:false}));sp.scale.set(scale*3.2,scale,1);return sp;}

function buildScene(T){
 scene=new THREE.Scene();meshes=[];memStars=[];skillStars=[];route=[];
 scene.add(new THREE.AmbientLight(0xffffff,.8));const pl=new THREE.PointLight(0xffffff,.7);pl.position.set(10,14,16);scene.add(pl);
 {const g=new THREE.BufferGeometry(),P=[];for(let i=0;i<1200;i++){const r=70+Math.random()*150,th=Math.random()*6.28,ph=Math.acos(2*Math.random()-1);
  P.push(r*Math.sin(ph)*Math.cos(th),r*Math.cos(ph),r*Math.sin(ph)*Math.sin(th));}g.setAttribute('position',new THREE.Float32BufferAttribute(P,3));
  scene.add(new THREE.Points(g,new THREE.PointsMaterial({color:0x223049,size:.5})));}

 route.push({label:'Query',kind:'query',col:COL.query,info:T.query});
 if(T.thinking) route.push({label:'Model reasoning',kind:'think',col:COL.think,info:(T.decision?'<b>Decision:</b> '+T.decision+'\n\n':'')+T.thinking});
 (T.actions||[]).forEach(a=>route.push({label:a.label,kind:a.is_skill?'skill':'action',col:a.error?COL.err:(a.is_skill?COL.skill:COL.action),
   error:a.error,tokens:a.tokens_out,ms:a.ms,info:'<b>'+a.tool+'</b>'+(a.error?' <span class="err">⚠ error</span>\n'+a.error_msg:'')+'\n<pre>'+(a.detail||'').replace(/</g,'&lt;')+'</pre>'}));
 route.push({label:'Answer',kind:'answer',col:COL.answer,info:T.answer});

 const N=route.length,H=Math.max(N*2.0,8),R=5.2;
 route.forEach((n,i)=>{const t=N>1?i/(N-1):0;n.pos=new THREE.Vector3(Math.sin(t*Math.PI*1.15)*R,(t-.5)*H,Math.cos(t*Math.PI*1.15)*R*.85-1.5);});
 const maxTok=Math.max(1,...route.map(n=>n.tokens||0));
 route.forEach((n,i)=>{const ei=.45+.9*((n.tokens||0)/maxTok);
  const s=new THREE.Mesh(new THREE.SphereGeometry(.42,32,32),new THREE.MeshStandardMaterial({color:n.col,emissive:n.col,emissiveIntensity:ei,roughness:.35}));
  s.position.copy(n.pos);s.userData={route:i};scene.add(s);meshes.push(s);
  if(n.error){const ring=new THREE.Mesh(new THREE.TorusGeometry(.62,.05,12,40),new THREE.MeshBasicMaterial({color:COL.err}));ring.position.copy(n.pos);scene.add(ring);}
  const lab=labelSprite((i+1)+'. '+n.label,.62,n.error?'#ff9a93':'#e9eef3',true);lab.position.copy(n.pos.clone().add(new THREE.Vector3(0,.95,0)));scene.add(lab);
  if(n.tokens||n.ms){const m=labelSprite((n.tokens?kfmt(n.tokens)+' tok ':'')+(n.ms?'· '+tfmt(n.ms):''),.34,'#6b7785');m.position.copy(n.pos.clone().add(new THREE.Vector3(0,-.78,0)));m.userData={metric:1};m.visible=!document.body.classList.contains('simple');scene.add(m);}});
 for(let i=0;i<route.length-1;i++){const a=route[i].pos,b=route[i+1].pos,d=b.clone().sub(a);scene.add(new THREE.ArrowHelper(d.clone().normalize(),a,d.length()-.5,0x6e7f96,.5,.28));}
 if(T.decision&&route.length>1){const a=route[0].pos,b=route[1].pos;const dl=labelSprite('💭 '+T.decision,.42,'#cdb4ff');dl.position.copy(a.clone().lerp(b,.5).add(new THREE.Vector3(2.4,0,0)));dl.scale.multiplyScalar(1.3);scene.add(dl);}

 const recalled=new Set((T.memory_recalled||[]).map(x=>x.toLowerCase()));
 function scatter(list,color,isMem){const arr=[];(list||[]).forEach(name=>{const hot=isMem?recalled.has(name.toLowerCase()):(name.toLowerCase()===(T.skill_used||'').toLowerCase());
   const r=11+Math.random()*9,th=Math.random()*6.28,ph=Math.acos(2*Math.random()-1);
   const v=new THREE.Vector3(r*Math.sin(ph)*Math.cos(th),(Math.random()-.5)*H*1.5,r*Math.sin(ph)*Math.sin(th)*.85);
   const m=new THREE.Mesh(new THREE.SphereGeometry(hot?.34:.15,16,16),new THREE.MeshStandardMaterial({color:color,emissive:color,emissiveIntensity:hot?.85:.16,roughness:.5,transparent:true,opacity:hot?1:.45}));
   m.position.copy(v);m.userData={star:name,mem:isMem,hot:hot};scene.add(m);arr.push(m);});return arr;}
 memStars=scatter(T.memory_titles,COL.mem,true);skillStars=scatter(T.skill_names,COL.skillStar,false);
 pickAll=meshes.concat(memStars,skillStars);

 curve=new THREE.CatmullRomCurve3(route.map(n=>n.pos));
 pulse=new THREE.Mesh(new THREE.SphereGeometry(.13,16,16),new THREE.MeshBasicMaterial({color:0xffffff}));scene.add(pulse);
 const box=new THREE.Box3().setFromPoints(route.map(n=>n.pos)),c2=box.getCenter(new THREE.Vector3());
 ctrl.target.copy(c2);cam.position.set(c2.x+9,c2.y+2,c2.z+15);
 buildPanel(T);
}

function buildPanel(T){
 const cost=((T.total_in||0)/1e6*15+(T.total_out||0)/1e6*75);
 document.getElementById('sum').innerHTML='<b>'+(T.model||'claude')+'</b> · steps: <b>'+route.length+'</b><br>tokens: in <b>'+kfmt(T.total_in||0)+'</b> · out <b>'+kfmt(T.total_out||0)+'</b>'+
  (T.total_ms?' · time <b>'+tfmt(T.total_ms)+'</b>':'')+'<br>≈ <b>$'+cost.toFixed(3)+'</b> <span style="color:#56606e">(approx. Opus rates)</span>'+
  (T.skill_used?'<br>skill: <b style="color:#7ee787">'+T.skill_used+'</b>':'<br><span style="color:#56606e">no skill invoked</span>')+
  (T.memory_recalled&&T.memory_recalled.length?'<br>memory surfaced: <b>'+T.memory_recalled.length+'</b> entr.':'<br><span style="color:#56606e">no memory surfaced in the log</span>');
 // story
 const s=[];s.push('You asked: "'+(T.query||'').slice(0,120)+'".');
 if(T.thinking)s.push('Claude thought it through and decided: '+(T.decision||'how best to answer')+'.');
 const acts=T.actions||[],sk=acts.filter(a=>a.is_skill),tl=acts.filter(a=>!a.is_skill);
 sk.forEach(a=>s.push('Engaged the "'+a.label.split(': ').pop()+'" skill — a helper for this kind of question.'));
 if(tl.length)s.push('Made '+tl.length+' '+(tl.length==1?'call':'calls')+' to data/files: '+tl.map(t=>'"'+t.label+'"').join(', ')+'.');
 const er=acts.filter(a=>a.error);if(er.length)s.push('⚠ '+er.length+' step(s) failed with an error.');
 if(T.memory_recalled&&T.memory_recalled.length)s.push('Recalled from memory: '+T.memory_recalled.slice(0,3).map(m=>'"'+m+'"').join('; ')+'.');
 else s.push('No memory surfaced for this query in the log.');
 if((T.answer||'').trim())s.push('Answered: '+(T.answer||'').split('\n')[0]);
 const stEl=document.getElementById('story');stEl.innerHTML='';s.forEach((p,i)=>{const d=document.createElement('p');d.dataset.i=(i+1);d.textContent=p;stEl.appendChild(d);});
 // steps
 const stepsEl=document.getElementById('steps');stepsEl.innerHTML='<h2 class="sec">Steps (in order)</h2>';
 route.forEach((n,i)=>{const met=(n.tokens?kfmt(n.tokens)+' tok ':'')+(n.ms?'· '+tfmt(n.ms):'')+(n.error?' · ⚠ error':'');
  const d=document.createElement('div');d.className='step';d.dataset.i=i;
  d.innerHTML='<div class="num" style="background:#'+n.col.toString(16).padStart(6,'0')+'">'+(i+1)+'</div><div><div class="lbl'+(n.error?' err':'')+'">'+n.label+'</div><div class="knd">'+n.kind+'</div>'+(met?'<div class="met">'+met+'</div>':'')+'</div>';
  d.onclick=()=>select(i);stepsEl.appendChild(d);});
 const diag=T.diagnosis||[];
 document.getElementById('fixbar').innerHTML=
  (diag.length?'<div style="color:#ff9a93;font-size:12px;margin-bottom:8px">⚠ '+diag.join('; ')+'</div>':'')+
  '<button class="addbtn" id="fixbtn" style="font-size:13px;padding:9px 16px">🔧 Fix this</button>';
 document.getElementById('fixbtn').onclick=()=>fixHandoff(T);
 document.getElementById('detail').style.display='none';
}
function fixHandoff(T){const payload={action:'diagnose_fix',query:T.query,diagnosis:T.diagnosis||[],
  route:(T.actions||[]).map(a=>a.label),errors:(T.actions||[]).filter(a=>a.error).map(a=>a.label+': '+a.error_msg),
  skill_used:T.skill_used||null,ts:T.ts||new Date().toISOString()};
 if(window.pywebview&&window.pywebview.api&&window.pywebview.api.submit){window.pywebview.api.submit(JSON.stringify(payload));}}
function select(i){document.querySelectorAll('.step').forEach(e=>e.classList.toggle('sel',+e.dataset.i===i));const n=route[i];
 document.getElementById('detail').style.display='block';document.getElementById('dttl').textContent=(i+1)+'. '+n.label;
 document.getElementById('dbody').innerHTML='<div>'+(n.info||'—').replace(/\n/g,'<br>')+'</div>';}
function showStar(u){document.getElementById('detail').style.display='block';document.getElementById('dttl').textContent=u.star;
 document.getElementById('dbody').innerHTML='<div class="src">source: config'+(u.mem?' (MEMORY.md)':' (~/.claude/skills)')+'</div>'+
  (u.hot?(u.mem?'<b>surfaced in this query</b> (was in system-reminder).':'<b>invoked in this query.</b>'):(u.mem?'was available but did not surface in the log.':'installed but not invoked.'));document.getElementById('detail').scrollIntoView({block:'nearest'});}
function pickAt(px,py){let best=null,bd=1e9;const v=new THREE.Vector3();pickAll.forEach(o=>{v.copy(o.position).project(cam);if(v.z>1)return;
  const sx=(v.x*.5+.5)*innerWidth,sy=(-v.y*.5+.5)*innerHeight,d=Math.hypot(sx-px,sy-py);if(d<bd){bd=d;best=o;}});
 if(!best||bd>44)return;const u=best.userData;if(u.route!=null)select(u.route);else if(u.ov)showOv(u);else showStar(u);}

// ---------- overview scene (memory + skills constellation) ----------
function buildOverview(O){
 scene=new THREE.Scene();meshes=[];memStars=[];skillStars=[];ovMeshes=[];route=[];curve=null;pulse=null;
 scene.add(new THREE.AmbientLight(0xffffff,.85));const pl=new THREE.PointLight(0xffffff,.7);pl.position.set(0,10,18);scene.add(pl);
 {const g=new THREE.BufferGeometry(),P=[];for(let i=0;i<1200;i++){const r=70+Math.random()*150,th=Math.random()*6.28,ph=Math.acos(2*Math.random()-1);P.push(r*Math.sin(ph)*Math.cos(th),r*Math.cos(ph),r*Math.sin(ph)*Math.sin(th));}g.setAttribute('position',new THREE.Float32BufferAttribute(P,3));scene.add(new THREE.Points(g,new THREE.PointsMaterial({color:0x223049,size:.5})));}
 const center=new THREE.Vector3(0,0,0);
 const cm=new THREE.Mesh(new THREE.SphereGeometry(.7,32,32),new THREE.MeshStandardMaterial({color:0x9aa7ff,emissive:0x6677dd,emissiveIntensity:.6,roughness:.3}));cm.position.copy(center);scene.add(cm);
 const cl=labelSprite('🧠 Claude',.8,'#e9eef3',true);cl.position.set(0,1.5,0);scene.add(cl);
 function place(list,side,color){const ms=Math.max(1,...list.map(x=>x.score||0));list.forEach((it,i)=>{
  const t=list.length>1?i/(list.length-1):.5,imp=(it.score||0)/ms,rad=5+(1-imp)*6;
  it.pos=new THREE.Vector3(side*(3.5+rad),(t-.5)*16,(Math.random()-.5)*9);const sz=.18+imp*.45;
  const m=new THREE.Mesh(new THREE.SphereGeometry(sz,18,18),new THREE.MeshStandardMaterial({color:color,emissive:color,emissiveIntensity:.3+imp*.6,roughness:.45,transparent:true,opacity:.55+imp*.45}));
  m.position.copy(it.pos);m.userData={ov:1,kind:side<0?'memory':'skill',name:it.name,type:it.type||'',desc:it.desc||'',score:it.score||0,links:it.links};scene.add(m);ovMeshes.push(m);
  const e=new THREE.BufferGeometry().setFromPoints([center,it.pos]);scene.add(new THREE.Line(e,new THREE.LineBasicMaterial({color:0x223049,transparent:true,opacity:.3})));
  if(imp>.55){const lb=labelSprite(it.name,.34,side<0?'#e6c07a':'#9fe0a8');lb.position.copy(it.pos.clone().add(new THREE.Vector3(0,sz+.45,0)));scene.add(lb);}});}
 place(O.memory,-1,0xd29922);place(O.skills,1,0x2ea043);
 const ml=labelSprite('MEMORY',.5,'#8a7340');ml.position.set(-12,8.5,0);scene.add(ml);
 const sl=labelSprite('SKILLS',.5,'#3d7a4d');sl.position.set(12,8.5,0);scene.add(sl);
 pickAll=ovMeshes;ctrl.target.set(0,0,0);cam.position.set(0,2,27);
 buildOvPanel(O);
}
function ovRow(it,kind,col){const d=document.createElement('div');d.className='step';
 const meta=kind==='memory'?((it.type||'—')+(it.links?' · links '+it.links:'')):('mentions '+(it.score||0));
 d.innerHTML='<div class="num" style="background:#'+col.toString(16).padStart(6,'0')+'"> </div><div><div class="lbl">'+it.name+'</div><div class="met">'+meta+'</div></div>';
 d.onclick=()=>showOv({name:it.name,kind:kind,type:it.type,desc:it.desc,score:it.score,links:it.links});return d;}
function buildOvPanel(O){
 document.getElementById('sum').innerHTML='<b>Claude\'s Mind</b> · project '+O.project+'<br>memory: <b>'+O.memory.length+'</b> key of '+O.mem_total+'<br>skills: <b>'+O.skills.length+'</b> of '+O.skill_total;
 document.getElementById('story').innerHTML='<div style="color:#9aa7b5;line-height:1.65">🟡 <b>Memory</b> — facts about you and the project; bigger star = more important (type + inbound links). 🟢 <b>Skills</b> — what Claude can invoke; bigger = used more often.<br>Click a star or a row below for details.</div>';
 const steps=document.getElementById('steps');steps.innerHTML='<h2 class="sec">Memory (key)</h2>';
 O.memory.forEach(it=>steps.appendChild(ovRow(it,'memory',0xd29922)));
 const h=document.createElement('h2');h.className='sec';h.textContent='Skills (key)';h.style.marginTop='12px';steps.appendChild(h);
 O.skills.forEach(it=>steps.appendChild(ovRow(it,'skill',0x2ea043)));
 document.getElementById('detail').style.display='none';document.getElementById('techblk').style.display='block';document.getElementById('fixbar').innerHTML='';
}
function showOv(u){document.getElementById('detail').style.display='block';document.getElementById('dttl').textContent=u.name;
 document.getElementById('dbody').innerHTML='<div class="src">'+(u.kind==='memory'?('memory'+(u.type?' · type '+u.type:'')+(u.links?' · inbound links '+u.links:'')):('skill'+(u.score?' · mentions '+u.score:'')))+'</div>'+(u.desc||'—');document.getElementById('detail').scrollIntoView({block:'nearest'});}

// ---------- navigation ----------
const $=id=>document.getElementById(id);
let liveTimer=null;
function stopLive(){if(liveTimer){clearInterval(liveTimer);liveTimer=null;}}
function hideMapUI(){stopLive();['back','panel','leg','hint'].forEach(id=>$(id).style.display='none');}
function showMapUI(legend){$('back').style.display='block';$('panel').style.display='block';$('hint').style.display='block';$('leg').style.display=legend?'block':'none';}
function hideScreens(){['menu','picker','addscreen','spendscreen'].forEach(id=>$(id).style.display='none');}
function showMenu(){stopLive();hideMapUI();hideScreens();$('menu').style.display='flex';}
function openPicker(){hideMapUI();hideScreens();$('picker').style.display='block';}
function openTrace(i){stopLive();hideScreens();document.querySelector('.tabs').style.display='flex';$('techblk').style.display='';backTo='picker';showMapUI(true);buildScene(TRACES[i]);}
function openOverview(){stopLive();hideScreens();document.querySelector('.tabs').style.display='none';backTo='menu';showMapUI(false);buildOverview(OVERVIEW);}
function openSpend(){hideMapUI();hideScreens();$('spendscreen').style.display='block';renderSpend();}
function openLive(){stopLive();hideScreens();document.querySelector('.tabs').style.display='flex';$('techblk').style.display='';backTo='menu';showMapUI(true);pollLive();liveTimer=setInterval(pollLive,2500);}
$('cRecent').onclick=openPicker;$('cOverview').onclick=openOverview;$('cSpend').onclick=openSpend;$('cLive').onclick=openLive;
$('pBack').onclick=showMenu;$('sBack').onclick=showMenu;
$('back').onclick=()=>{hideMapUI();if(backTo==='picker')openPicker();else showMenu();};
// spend dashboard
function fmtUsd(n){return '$'+(n<1?n.toFixed(3):n.toFixed(2));}
function kpi(v,l){return '<div class="kpi"><div class="v">'+v+'</div><div class="l">'+l+'</div></div>';}
function bar(name,val,max,label,tag){return '<div class="barrow"><div class="nm" title="'+String(name).replace(/"/g,'')+'">'+String(name).replace(/</g,'&lt;')+(tag?' · <span style="color:#7ee787">'+tag+'</span>':'')+'</div><div class="bar"><i style="width:'+Math.max(3,val/max*100)+'%"></i></div><div class="cv">'+label+'</div></div>';}
function renderSpend(){const S=SPEND;
 $('kpis').innerHTML=kpi(fmtUsd(S.cost),'≈ cost · '+S.queries+' queries / '+S.files+' sessions')+
  kpi(kfmt(S.in+S.cache)+' → '+kfmt(S.out),'tokens in → out')+
  kpi((S.cache_rate*100).toFixed(0)+'%','cache-hit (cache is 10× cheaper)')+
  kpi((S.ms/1000).toFixed(0)+'s','total time');
 const mc=Math.max(1,...S.top.map(r=>r.cost));
 $('topq').innerHTML=S.top.map(r=>bar(r.query,r.cost,mc,fmtUsd(r.cost),r.skill||'')).join('')||'<div style="color:#56606e">no data</div>';
 const ms=Math.max(1,...S.per_skill.map(d=>d.cost));
 $('perskill').innerHTML=S.per_skill.map(d=>bar(d.skill+' · '+d.count+' q.',d.cost,ms,fmtUsd(d.cost),'')).join('');}
// live mode
function pollLive(){if(!(window.pywebview&&window.pywebview.api&&window.pywebview.api.latest))return;
 window.pywebview.api.latest().then(s=>{if(!s||s==='null')return;let T;try{T=JSON.parse(s);}catch(e){return;}
  const sig=(T.actions||[]).length+'|'+(T.answer||'').length+'|'+(T.thinking||'').length+'|'+(T.query||'').length;
  if(sig!==window._liveSig){window._liveSig=sig;buildScene(T);}});}
// add memory/skill form
let addKind='memory';
function openAdd(){$('menu').style.display='none';$('addscreen').style.display='block';$('aErr').style.display='none';}
$('cAdd').onclick=openAdd;$('aBack').onclick=showMenu;
$('kMem').onclick=()=>{addKind='memory';$('kMem').classList.add('on');$('kSkill').classList.remove('on');};
$('kSkill').onclick=()=>{addKind='skill';$('kSkill').classList.add('on');$('kMem').classList.remove('on');};
$('aSubmit').onclick=()=>{const name=$('aName').value.trim(),desc=$('aDesc').value.trim();
 if(!name||!desc){$('aErr').textContent='Fill in both the name and the description.';$('aErr').style.display='block';return;}
 const payload={action:addKind==='skill'?'add_skill':'add_memory',kind:addKind,name:name,description:desc,ts:new Date().toISOString()};
 if(window.pywebview&&window.pywebview.api&&window.pywebview.api.submit){$('aSubmit').textContent='Handing off to Claude…';$('aSubmit').disabled=true;window.pywebview.api.submit(JSON.stringify(payload));}
 else{$('aErr').textContent='No connection to the app — launch it via /trace_map.';$('aErr').style.display='block';}};
const tS=$('tSimple'),tT=$('tTech');
function mode(simple){document.body.classList.toggle('simple',simple);tS.classList.toggle('on',simple);tT.classList.toggle('on',!simple);
 if(scene)scene.traverse(o=>{if(o.userData&&o.userData.metric)o.visible=!simple;});}
tS.onclick=()=>mode(true);tT.onclick=()=>mode(false);

init3D();mode(true);showMenu();
</script></body></html>"""

def _embed(obj):
    # escape "</" so a literal </script> inside the data can't close the inline tag
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")

def render_app(traces, overview, spend, out=str(OUTDIR / "trace_app.html")):
    # split the TEMPLATE (no data yet → each token appears exactly once) and join by
    # concatenation, so data that happens to contain the tokens can't collide.
    a, r1 = APP_HTML.split("__TRACES__", 1)
    b, r2 = r1.split("__OVERVIEW__", 1)
    c, d = r2.split("__SPEND__", 1)
    html = a + _embed(traces) + b + _embed(overview) + c + _embed(spend) + d
    Path(out).write_text(html, encoding="utf-8")
    return out

if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 10
    traces = recent_traces(n)
    overview = build_overview()
    spend = build_spend()
    out = render_app(traces, overview, spend)
    print(f"overview: {len(overview['memory'])}/{overview['mem_total']} memory · {len(overview['skills'])}/{overview['skill_total']} skills")
    print(f"spend: {spend['queries']} queries · ${spend['cost']:.2f} · cache {spend['cache_rate']*100:.0f}%")
    print(f"queries: {len(traces)}")
    for i, t in enumerate(traces):
        print(f"  {i+1}. {t['ts'][:16]} | {t['skill_used'] or 'self'} | {t['query'][:60]!r}")
    print(f"OUT: {out}")
    try: os.remove(HANDOFF)
    except OSError: pass
    api = Api()
    _WINDOW = webview.create_window("🗺️ Trace Map", url="file://" + os.path.abspath(out),
                                    js_api=api, width=1360, height=880, min_size=(960, 660))
    webview.start()
    if Path(HANDOFF).exists():
        print("HANDOFF:", HANDOFF)
        print(Path(HANDOFF).read_text(encoding="utf-8"))
