---
name: trace-map
description: >-
  Opens a native 3D "star-field" window that visualizes, from real Claude Code
  session logs, the actual path a query took (query → reasoning → tool/skill
  calls → answer), a constellation of the assistant's memory & skills, a spend
  dashboard (tokens/time/$ per session and per skill), a live real-time view,
  and an "add memory/skill" + "fix this" hand-off back into the chat. Use when
  the user wants to see how Claude handled a request, what memory/skills it has,
  where tokens/money go, or to add/repair a memory or skill from a real run.
  Triggers: trace map, /trace_map, query route, what Claude did, how Claude
  answered, visualize memory and skills, Claude's mind, token spend, how much it
  cost, fix a skill/memory, add memory, add skill, live mode.
---

# Trace Map

A native desktop app (no browser tab) that turns **real Claude Code transcripts**
into an honest, interactive map of what the assistant actually did — plus
action hand-offs back into the chat.

## Setup

```bash
pip3 install -r "$(dirname "$0")/requirements.txt"   # pywebview (macOS WKWebView)
```

Three.js is loaded from a CDN, so the window needs internet on first open.

## Run it

Launch the app (this **blocks** until the window is closed — that's intended,
control returns here afterwards):

```bash
python3 <skill_dir>/scripts/trace_app.py
```

It opens a window with a main menu:

| Card | What it shows |
|---|---|
| 🧠 **Claude's Mind** | Constellation of the most important **memory** (gold, ranked by type + inbound `[[links]]`) and **skills** (green, ranked by recent usage). |
| 🛤️ **Recent queries** | The last ~10 real user queries → click one → its 3D route (query → reasoning → tool/skill calls → answer) with tokens/time/errors and a plain-language story. |
| 💰 **Spend** | Tokens / time / **≈$** across recent sessions, cache-hit rate, most expensive queries, breakdown by skill. |
| 🔴 **Live** | Polls the current transcript and rebuilds the route in real time as you talk to Claude. |
| ➕ **Add memory/skill** | A small form (type → name → description) that hands off to the chat. |

Honesty model: the **route, reasoning, tool/skill calls, tokens, errors** are
read straight from the transcript. The **memory/skill stars** are sourced from
the filesystem config (`MEMORY.md`, `~/.claude/skills`) and labelled as such —
the tool never pretends the log contains a routing decision it doesn't.

## After the window closes — handle the hand-off

When the user clicks **➕ Add** or **🔧 Fix this**, the app writes a
hand-off file and closes the window. The app prints `HANDOFF: <path>` on exit.
**As soon as the app exits, check for that file and act on it:**

```bash
python3 -c "import tempfile,os;p=os.path.join(tempfile.gettempdir(),'trace-map','handoff.json');print(open(p).read()) if os.path.exists(p) else print('NO_HANDOFF')"
```

Then, based on `action`:

- **`add_memory`** — create a memory file in the user's memory dir using the
  project's memory format (frontmatter `name`/`description`/`metadata.type`,
  body fact, `[[links]]`) and add a one-line pointer to `MEMORY.md`. Use the
  given `name`/`description` as a seed and ask follow-ups: which **type**
  (`user`/`feedback`/`project`/`reference`), the concrete **fact**, and what to
  **link**. Don't write until it's a real, non-trivial fact.
- **`add_skill`** — scaffold a new skill (a `SKILL.md` with frontmatter +
  instructions) from the `name`/`description`, asking what it should do and its
  trigger phrases. (If a skill-authoring skill is available, defer to it.)
- **`diagnose_fix`** — you receive `query`, `diagnosis`, `route`, `errors`,
  `skill_used`. Diagnose the **root cause** (missing/incorrect skill trigger,
  redundant tool calls, a tool error, an unused-but-relevant memory), propose a
  **minimal fix** to the skill / memory / prompt, apply it on approval, then
  offer to **re-run the same query** so a fresh `/trace_map` shows the improved
  route. Never invent a fix when `diagnosis` is empty and there are no errors —
  say there's nothing to repair.

After consuming the hand-off, delete it so it isn't re-read:

```bash
python3 -c "import tempfile,os;p=os.path.join(tempfile.gettempdir(),'trace-map','handoff.json');os.path.exists(p) and os.remove(p)"
```

If no hand-off file exists, the user just browsed and closed the window —
nothing to do.

## Notes

- macOS-first (pywebview uses Cocoa/WKWebView). On Linux/Windows pywebview uses
  the platform webview; the app logic is the same.
- `scripts/trace_map.py` can also render a single trace to HTML standalone:
  `python3 scripts/trace_map.py [session.jsonl]`.
