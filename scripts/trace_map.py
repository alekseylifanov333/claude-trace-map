#!/usr/bin/env python3
"""
trace_map.py — honest 3D "constellation" map of one Claude Code query.
Shows the explicit numbered route (query -> reasoning -> tool/skill calls ->
answer), plus, grounded in the transcript:
  (1) cost/effort  — tokens + time per step and a query total
  (2) success/error — failed tool calls turn red with the error text
  (3) real memory recalls — memories that actually surfaced (system-reminder) glow
  (4) decision     — the sentence from `thinking` where it chose a skill
  (6) narration    — a plain-language story of the route ("simple" mode)

Usage:
    python3 trace_map.py [session.jsonl]     # parse latest exchange
    python3 trace_map.py --json trace.json   # render a prepared trace
"""
import json, sys, os, re, tempfile
from pathlib import Path

OUTDIR = Path(tempfile.gettempdir()) / "trace-map"; OUTDIR.mkdir(exist_ok=True)

def latest_transcript():
    base = Path.home() / ".claude" / "projects"
    fs = sorted(base.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return fs[0] if fs else None

def load(path):
    out = []
    for line in open(path):
        line = line.strip()
        if line:
            try: out.append(json.loads(line))
            except Exception: pass
    return out

def role(o):
    m = o.get("message"); return m.get("role") if isinstance(m, dict) else None
def blocks(o):
    m = o.get("message"); return m["content"] if isinstance(m, dict) and isinstance(m.get("content"), list) else []
def usage(o):
    m = o.get("message"); return m.get("usage") if isinstance(m, dict) and isinstance(m.get("usage"), dict) else {}
def text_of(o):
    p = []
    for b in blocks(o):
        if isinstance(b, dict) and b.get("type") == "text": p.append(b.get("text", ""))
        elif isinstance(b, str): p.append(b)
    m = o.get("message")
    if isinstance(m, dict) and isinstance(m.get("content"), str): p.append(m["content"])
    return "\n".join(p).strip()
def is_user_turn(o):
    if o.get("type") != "user" or role(o) != "user": return False
    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks(o)): return False
    t = text_of(o)
    return bool(t) and not t.startswith("<local-command") and not t.startswith("<command-")
def trim(s, n=600):
    s = (s or "").strip(); return s if len(s) <= n else s[:n-1] + "…"

def context_signals(transcript_path):
    proj = Path(transcript_path).parent.name
    titles = []
    md = Path.home() / ".claude" / "projects" / proj / "memory" / "MEMORY.md"
    if md.exists():
        titles = [m.group(1) for m in re.finditer(r"^\s*- \[([^\]]+)\]", md.read_text(encoding="utf-8", errors="ignore"), re.M)]
    sd = Path.home() / ".claude" / "skills"
    skills = sorted(d.name for d in sd.iterdir() if d.is_dir()) if sd.exists() else []
    return titles, skills

def extract_decision(thinking):
    for s in re.split(r"(?<=[.!?。])\s+", thinking or ""):
        if re.search(r"(скилл|skill|подключ|вызов|позва|use the|invoke|воспользу)", s, re.I):
            return trim(s, 220)
    return ""

def last_exchange(rows, mem_titles):
    last = None
    for i, o in enumerate(rows):
        if is_user_turn(o): last = i
    if last is None: return None
    query = text_of(rows[last])
    thinking, answer, actions = [], [], []
    id2idx = {}                # tool_use_id -> action index
    recalled = set()
    total_out = total_in = total_ms = 0
    model = ""
    grp = []                   # action indices of the current assistant message (for turn_duration)
    for o in rows[last+1:]:
        if is_user_turn(o): break
        t = o.get("type")
        if t == "assistant":
            u = usage(o); out = u.get("output_tokens", 0) or 0
            inn = (u.get("input_tokens", 0) or 0) + (u.get("cache_read_input_tokens", 0) or 0)
            total_out += out; total_in = max(total_in, inn)
            m = o.get("message")
            if isinstance(m, dict) and m.get("model") and m["model"] != "<synthetic>": model = m["model"]
            tu = [b for b in blocks(o) if isinstance(b, dict) and b.get("type") == "tool_use"]
            per = round(out / len(tu)) if tu else 0
            grp = []
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
        elif t == "user":  # tool_result turn + possible system-reminders
            for b in blocks(o):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    if b.get("is_error"):
                        idx = id2idx.get(b.get("tool_use_id"))
                        if idx is not None:
                            actions[idx]["error"] = True
                            c = b.get("content")
                            actions[idx]["error_msg"] = trim(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False), 200)
            txt = text_of(o)
            if "system-reminder" in txt:
                for title in mem_titles:
                    if title[:24].lower() in txt.lower(): recalled.add(title)
    return {"query": trim(query, 800), "thinking": trim("\n\n".join(thinking), 1400),
            "decision": extract_decision("\n".join(thinking)), "actions": actions,
            "answer": trim("\n\n".join(answer), 1200), "memory_recalled": sorted(recalled),
            "total_out": total_out, "total_in": total_in, "total_ms": total_ms, "model": model}

def build_trace(path):
    rows = load(path); mem_titles, skill_names = context_signals(path)
    ex = last_exchange(rows, mem_titles)
    if not ex: raise SystemExit("No user query found.")
    used = next((a["label"].split(": ", 1)[-1] for a in ex["actions"] if a["is_skill"]), None)
    return {"session": os.path.basename(path), "memory_titles": mem_titles,
            "skill_names": skill_names, "skill_used": used, **ex}

# ============================ RENDER ============================
HTML_TMPL = r"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Claude Trace Map</title>
<style>
 html,body{margin:0;height:100%;background:#05070d;color:#e6edf3;font:14px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;overflow:hidden}
 #panel{position:fixed;top:0;right:0;width:360px;height:100vh;box-sizing:border-box;overflow:auto;padding:14px 18px;
        background:rgba(8,12,20,.92);border-left:1px solid #182030;backdrop-filter:blur(8px)}
 .tabs{display:flex;gap:6px;margin-bottom:12px}
 .tab{flex:1;text-align:center;padding:7px;border-radius:8px;background:#0e1422;border:1px solid #182030;cursor:pointer;font-size:12px;color:#9aa7b5}
 .tab.on{background:#15233a;color:#e6edf3;border-color:#2b4a6e}
 .sum{font-size:12px;color:#9aa7b5;background:#0a1018;border:1px solid #182030;border-radius:8px;padding:9px 11px;margin-bottom:12px;line-height:1.7}
 .sum b{color:#e6edf3}
 #story{font-size:13.5px;line-height:1.7;margin-bottom:14px}
 #story p{margin:0 0 8px;padding-left:22px;position:relative}
 #story p:before{content:attr(data-i);position:absolute;left:0;top:1px;color:#56606e;font-size:11px}
 h1{font-size:12px;margin:4px 0 6px;color:#9aa7b5;font-weight:600;text-transform:uppercase;letter-spacing:.5px}
 .step{display:flex;gap:9px;align-items:flex-start;padding:7px 8px;border-radius:8px;cursor:pointer;margin-bottom:3px;border:1px solid transparent}
 .step:hover{background:#0e1422}.step.sel{background:#0e1726;border-color:#1f3350}
 .num{flex:0 0 20px;height:20px;border-radius:50%;font-size:11px;display:flex;align-items:center;justify-content:center;color:#05070d;font-weight:700}
 .lbl{font-size:13px}.knd{font-size:10px;color:#56606e}
 .met{font-size:10px;color:#6b7785;margin-top:2px}
 .err{color:#ff7b72}
 #detail{margin-top:12px;border-top:1px solid #182030;padding-top:12px;display:none}
 #detail h2{font-size:13px;margin:0 0 6px}#detail pre{white-space:pre-wrap;word-break:break-word;background:#0a0f18;border:1px solid #182030;border-radius:6px;padding:9px;font-size:11px;color:#aeb9c4;max-height:40vh;overflow:auto}
 .src{font-size:10px;color:#d29922;margin-top:4px}
 body.simple .knd,body.simple .met,body.simple #techblk{display:none}
 #hint{position:fixed;left:16px;bottom:14px;color:#56606e;font-size:11px}
 #title{position:fixed;left:18px;top:14px;font-size:12px;color:#56606e}
 .leg{position:fixed;left:16px;bottom:40px;font-size:11px;color:#7d8590}
 .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin:0 5px 0 12px;vertical-align:middle}
</style></head><body>
<div id="title">🗺️ Query route</div>
<div class="leg">
 <span class="dot" style="background:#58a6ff"></span>query<span class="dot" style="background:#bc8cff"></span>thinking
 <span class="dot" style="background:#7ee787"></span>skill<span class="dot" style="background:#ff7b72"></span>tool
 <span class="dot" style="background:#39c5cf"></span>answer<span class="dot" style="background:#d29922"></span>memory</div>
<div id="hint">drag — rotate · wheel — zoom · click a step/star — details</div>
<div id="panel">
 <div class="tabs"><div class="tab on" id="tSimple">📖 Simple</div><div class="tab" id="tTech">⚙️ Technical</div></div>
 <div class="sum" id="sum"></div>
 <div id="story"></div>
 <div id="techblk"><h1>Steps (in order)</h1><div id="steps"></div></div>
 <div id="detail"><h2 id="dttl"></h2><div id="dbody"></div></div>
</div>
<script src="https://unpkg.com/three@0.128.0/build/three.min.js"></script>
<script src="https://unpkg.com/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
<script>
const T=__TRACE__;
const COL={query:0x58a6ff,think:0xbc8cff,skill:0x7ee787,action:0xff7b72,answer:0x39c5cf,mem:0xd29922,skillStar:0x2ea043,err:0xf85149};
const kfmt=n=>n>=1000?(n/1000).toFixed(1)+'k':String(n);
const tfmt=ms=>ms?(ms/1000).toFixed(1)+'s':'';

// ---- route ----
const route=[];
route.push({label:'Query',kind:'query',col:COL.query,info:T.query});
if(T.thinking) route.push({label:'Model reasoning',kind:'think',col:COL.think,
   info:(T.decision?'<b>Decision:</b> '+T.decision+'\n\n':'')+T.thinking});
(T.actions||[]).forEach(a=>route.push({label:a.label,kind:a.is_skill?'skill':'action',
   col:a.error?COL.err:(a.is_skill?COL.skill:COL.action),error:a.error,tokens:a.tokens_out,ms:a.ms,
   info:'<b>'+a.tool+'</b>'+(a.error?' <span class="err">⚠ error</span>\n'+a.error_msg:'')+'\n<pre>'+a.detail.replace(/</g,'&lt;')+'</pre>',
   skillName:a.is_skill?a.label.split(': ').pop():null}));
route.push({label:'Answer',kind:'answer',col:COL.answer,info:T.answer});

// ---- summary ----
const cost=((T.total_in||0)/1e6*15+(T.total_out||0)/1e6*75);
document.getElementById('sum').innerHTML=
 '<b>'+(T.model||'claude')+'</b> · steps: <b>'+route.length+'</b><br>'+
 'tokens: in <b>'+kfmt(T.total_in||0)+'</b> · out <b>'+kfmt(T.total_out||0)+'</b>'+
 (T.total_ms?' · time <b>'+tfmt(T.total_ms)+'</b>':'')+'<br>'+
 '≈ <b>$'+cost.toFixed(3)+'</b> <span style="color:#56606e">(approx. Opus rates)</span>'+
 (T.skill_used?'<br>skill: <b style="color:#7ee787">'+T.skill_used+'</b>':'<br><span style="color:#56606e">no skill invoked</span>')+
 (T.memory_recalled&&T.memory_recalled.length?'<br>memory surfaced: <b>'+T.memory_recalled.length+'</b> entr.':'<br><span style="color:#56606e">no memory surfaced in the log</span>');

// ---- narration (6) ----
function story(){
 const s=[]; s.push('You asked: "'+(T.query||'').slice(0,120)+'".');
 if(T.thinking) s.push('Claude thought it through and decided: '+(T.decision||'how best to answer')+'.');
 const acts=T.actions||[];
 const skills=acts.filter(a=>a.is_skill), tools=acts.filter(a=>!a.is_skill);
 skills.forEach(a=>s.push('Engaged the "'+a.label.split(': ').pop()+'" skill — a helper for this kind of question.'));
 if(tools.length) s.push('Made '+tools.length+' '+(tools.length==1?'call':'calls')+' to data: '+tools.map(t=>'"'+t.label+'"').join(', ')+'.');
 const errs=acts.filter(a=>a.error); if(errs.length) s.push('⚠ '+errs.length+' step(s) failed with an error and were retried.');
 if(T.memory_recalled&&T.memory_recalled.length) s.push('Recalled from memory: '+T.memory_recalled.slice(0,3).map(m=>'"'+m+'"').join('; ')+'.');
 else s.push('No memory was needed for this query — the answer came from the skill call and the data.');
 s.push('Answered: '+(T.answer||'').split('\n')[0]);
 return s;
}
const stEl=document.getElementById('story');
story().forEach((p,i)=>{const d=document.createElement('p');d.dataset.i=(i+1);d.textContent=p;stEl.appendChild(d);});

// ---- 3D ----
const scene=new THREE.Scene();
const cam=new THREE.PerspectiveCamera(52,innerWidth/innerHeight,.1,500);
const rnd=new THREE.WebGLRenderer({antialias:true});rnd.setSize(innerWidth,innerHeight);rnd.setPixelRatio(devicePixelRatio);
document.body.appendChild(rnd.domElement);
const ctrl=new THREE.OrbitControls(cam,rnd.domElement);ctrl.enableDamping=true;
scene.add(new THREE.AmbientLight(0xffffff,.8));const pl=new THREE.PointLight(0xffffff,.7);pl.position.set(10,14,16);scene.add(pl);
(function(){const g=new THREE.BufferGeometry(),P=[];for(let i=0;i<1400;i++){const r=70+Math.random()*140,th=Math.random()*6.28,ph=Math.acos(2*Math.random()-1);
 P.push(r*Math.sin(ph)*Math.cos(th),r*Math.cos(ph),r*Math.sin(ph)*Math.sin(th));}
 g.setAttribute('position',new THREE.Float32BufferAttribute(P,3));scene.add(new THREE.Points(g,new THREE.PointsMaterial({color:0x223049,size:.5})));})();

const N=route.length,H=Math.max(N*2.0,8),R=5.2;
route.forEach((n,i)=>{const t=N>1?i/(N-1):0;
 n.pos=new THREE.Vector3(Math.sin(t*Math.PI*1.15)*R,(t-.5)*H,Math.cos(t*Math.PI*1.15)*R*.85-1.5);});

function labelSprite(text,scale,color,bold){const c=document.createElement('canvas');c.width=512;c.height=160;const g=c.getContext('2d');
 g.fillStyle=color;g.font=(bold?'bold ':'')+'40px -apple-system,Segoe UI,sans-serif';g.textAlign='center';g.textBaseline='middle';
 const L=String(text).match(/.{1,22}(\s|$)/g)||[String(text)];L.slice(0,3).forEach((ln,k,a)=>g.fillText(ln.trim(),256,80+(k-(a.length-1)/2)*46));
 const sp=new THREE.Sprite(new THREE.SpriteMaterial({map:new THREE.CanvasTexture(c),transparent:true,depthWrite:false}));sp.scale.set(scale*3.2,scale,1);return sp;}

const meshes=[];const maxTok=Math.max(1,...route.map(n=>n.tokens||0));
route.forEach((n,i)=>{
 const ei=.45+.9*((n.tokens||0)/maxTok);                       // (1) emissive scales with tokens/effort
 const s=new THREE.Mesh(new THREE.SphereGeometry(.42,32,32),
   new THREE.MeshStandardMaterial({color:n.col,emissive:n.col,emissiveIntensity:ei,roughness:.35}));
 s.position.copy(n.pos);s.userData={route:i};scene.add(s);meshes.push(s);
 if(n.error){const ring=new THREE.Mesh(new THREE.TorusGeometry(.62,.05,12,40),new THREE.MeshBasicMaterial({color:COL.err}));
   ring.position.copy(n.pos);scene.add(ring);}                  // (2) red ring on errors
 const lab=labelSprite((i+1)+'. '+n.label,.62,n.error?'#ff9a93':'#e9eef3',true);lab.position.copy(n.pos.clone().add(new THREE.Vector3(0,.95,0)));scene.add(lab);
 if((n.tokens||n.ms)){const m=labelSprite((n.tokens?kfmt(n.tokens)+' tok ':'')+(n.ms?'· '+tfmt(n.ms):''),.34,'#6b7785');
   m.position.copy(n.pos.clone().add(new THREE.Vector3(0,-.78,0)));m.userData={metric:1};scene.add(m);}
});
for(let i=0;i<route.length-1;i++){const a=route[i].pos,b=route[i+1].pos,d=b.clone().sub(a);
 scene.add(new THREE.ArrowHelper(d.clone().normalize(),a,d.length()-.5,0x6e7f96,.5,.28));}

// (4) decision label on the arrow leaving the query
if(T.decision&&route.length>1){const a=route[0].pos,b=route[1].pos;
 const dl=labelSprite('💭 '+T.decision,.42,'#cdb4ff');dl.position.copy(a.clone().lerp(b,.5).add(new THREE.Vector3(2.4,0,0)));dl.scale.multiplyScalar(1.3);scene.add(dl);}

// (3) memory & skills as stars; recalled memory / used skill glow
const recalled=new Set((T.memory_recalled||[]).map(x=>x.toLowerCase()));
function scatter(list,color,isMem){const arr=[];list.forEach(name=>{
 const hot=isMem?recalled.has(name.toLowerCase()):(name.toLowerCase()===(T.skill_used||'').toLowerCase());
 const r=11+Math.random()*9,th=Math.random()*6.28,ph=Math.acos(2*Math.random()-1);
 const v=new THREE.Vector3(r*Math.sin(ph)*Math.cos(th),(Math.random()-.5)*H*1.5,r*Math.sin(ph)*Math.sin(th)*.85);
 const m=new THREE.Mesh(new THREE.SphereGeometry(hot?.34:.15,16,16),
   new THREE.MeshStandardMaterial({color:color,emissive:color,emissiveIntensity:hot?.85:.16,roughness:.5,transparent:true,opacity:hot?1:.45}));
 m.position.copy(v);m.userData={star:name,mem:isMem,hot:hot};scene.add(m);arr.push(m);});return arr;}
const memStars=scatter(T.memory_titles||[],COL.mem,true);
const skillStars=scatter(T.skill_names||[],COL.skillStar,false);

// panel steps
const stepsEl=document.getElementById('steps');
route.forEach((n,i)=>{const d=document.createElement('div');d.className='step';d.dataset.i=i;
 const met=(n.tokens?kfmt(n.tokens)+' tok ':'')+(n.ms?'· '+tfmt(n.ms):'')+(n.error?' · ⚠ error':'');
 d.innerHTML='<div class="num" style="background:#'+n.col.toString(16).padStart(6,'0')+'">'+(i+1)+'</div>'+
  '<div><div class="lbl'+(n.error?' err':'')+'">'+n.label+'</div><div class="knd">'+n.kind+'</div>'+(met?'<div class="met">'+met+'</div>':'')+'</div>';
 d.onclick=()=>select(i);stepsEl.appendChild(d);});
function select(i){document.querySelectorAll('.step').forEach(e=>e.classList.toggle('sel',+e.dataset.i===i));
 const n=route[i];document.getElementById('detail').style.display='block';
 document.getElementById('dttl').textContent=(i+1)+'. '+n.label;
 document.getElementById('dbody').innerHTML='<div>'+(n.info||'—').replace(/\n/g,'<br>')+'</div>';}

const curve=new THREE.CatmullRomCurve3(route.map(n=>n.pos));
const pulse=new THREE.Mesh(new THREE.SphereGeometry(.13,16,16),new THREE.MeshBasicMaterial({color:0xffffff}));scene.add(pulse);
const box=new THREE.Box3().setFromPoints(route.map(n=>n.pos)),c2=box.getCenter(new THREE.Vector3());
ctrl.target.copy(c2);cam.position.set(c2.x+9,c2.y+2,c2.z+15);

function showStar(u){document.getElementById('detail').style.display='block';document.getElementById('dttl').textContent=u.star;
 document.getElementById('dbody').innerHTML='<div class="src">source: config'+(u.mem?' (MEMORY.md)':' (~/.claude/skills)')+'</div>'+
  (u.hot?(u.mem?'<b>surfaced in this query</b> (was in system-reminder).':'<b>invoked in this query.</b>'):
         (u.mem?'was available but did not surface in the log.':'installed but not invoked.'));}
// Screen-space picking: project every node to pixels and take the nearest within
// a generous radius — robust for tiny far stars and reliable in WKWebView.
const pickable=()=>meshes.concat(memStars,skillStars);
function pickAt(px,py){let best=null,bd=1e9;const v=new THREE.Vector3();
 pickable().forEach(o=>{v.copy(o.position).project(cam);if(v.z>1)return;
  const sx=(v.x*.5+.5)*innerWidth,sy=(-v.y*.5+.5)*innerHeight,d=Math.hypot(sx-px,sy-py);
  if(d<bd){bd=d;best=o;}});
 if(!best||bd>40)return;const u=best.userData;
 if(u.route!=null)select(u.route);else showStar(u);}
// capture-phase on window: fires before OrbitControls and cannot be swallowed.
let _dn=null;
addEventListener('pointerdown',e=>{_dn=[e.clientX,e.clientY];},true);
addEventListener('pointerup',e=>{if(e.clientX>innerWidth-360){_dn=null;return;}
 if(!_dn)return;const moved=Math.hypot(e.clientX-_dn[0],e.clientY-_dn[1]);_dn=null;if(moved<6)pickAt(e.clientX,e.clientY);},true);

let t=0;function loop(){requestAnimationFrame(loop);t=(t+.0025)%1;pulse.position.copy(curve.getPointAt(t));ctrl.update();rnd.render(scene,cam);}loop();
addEventListener('resize',()=>{cam.aspect=innerWidth/innerHeight;cam.updateProjectionMatrix();rnd.setSize(innerWidth,innerHeight);});

// mode toggle (6)
const tS=document.getElementById('tSimple'),tT=document.getElementById('tTech');
function mode(simple){document.body.classList.toggle('simple',simple);tS.classList.toggle('on',simple);tT.classList.toggle('on',!simple);
 meshes.forEach(()=>{});scene.traverse(o=>{if(o.userData&&o.userData.metric)o.visible=!simple;});}
tS.onclick=()=>mode(true);tT.onclick=()=>mode(false);
select(0);mode(true);
</script></body></html>"""

def render(trace, out=str(OUTDIR / "trace.html")):
    Path(out).write_text(HTML_TMPL.replace("__TRACE__", json.dumps(trace, ensure_ascii=False)), encoding="utf-8")
    return out

if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--json":
        trace = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        if "skill_names" not in trace:
            mt, sn = context_signals(str(latest_transcript()))
            trace.setdefault("memory_titles", mt); trace.setdefault("skill_names", sn)
    else:
        src = sys.argv[1] if len(sys.argv) > 1 else latest_transcript()
        trace = build_trace(str(src)); print(f"source : {src}")
    out = render(trace)
    print(f"query  : {trace['query'][:70]}")
    print(f"route  : {len(trace['actions'])+ (2 if trace.get('thinking') else 1) +1} steps | tok in/out: {trace.get('total_in')}/{trace.get('total_out')} | {trace.get('total_ms')}ms")
    print(f"recall : {trace.get('memory_recalled')} | errors: {sum(1 for a in trace['actions'] if a.get('error'))} | used: {trace.get('skill_used')}")
    print(f"OUT    : {out}")
