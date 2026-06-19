# Session 10 — Computer-Use Agent (Desktop Cascade)

A **computer-use** agent that drives real macOS desktop applications to
completion and emits a turn-numbered trajectory for each run. It is the
desktop twin of the Session 9 Browser skill: where Browser drives a web page
through `extract → deterministic → a11y → vision`, the **Computer** skill
drives a *desktop app* through the same cost cascade retargeted from the DOM
to the OS accessibility tree (plus an Electron/CDP tier):

| Layer | Tier | Mechanism | Cost |
| :-- | :-- | :-- | :-- |
| 1 | `extract` | read-only AX text / clipboard, no click, no LLM | $0 |
| 2a | `hotkey` | deterministic key sequence, verified via clipboard | $0 |
| 2b | `a11y` | AX-tree walk + a cheap text-LLM judge picks an element | ¢ |
| 2* | `cdp` | Electron page tool over Chrome DevTools Protocol | ¢ |
| 3 | `vision` | screenshot + set-of-marks + V9 `/v1/vision` | $$ |

The orchestrator (`flow.py`) is **not** modified. The Computer skill plugs in
through the skill catalogue (`agent_config.yaml`), exactly like the Browser
skill. Each tier is attempted in order and **escalates** only when it cannot
satisfy the goal; the skill stops at the first tier that succeeds.
`output.path` is the tier that produced the answer and `output.cascade` lists
every tier attempted.

> **Safety.** Targets are stateless system apps (Calculator), public no-login
> pages (a browser game), and read-only workbench state (VS Code). The skill
> never opens the user's documents or authenticated sessions.

---

## The three assignment queries

Each query is run through the full orchestrator with `flow.py` and lands on a
**different** cascade tier — proving the desktop cost cascade routes itself.

| # | Query | App | Tier reached | Session |
| :-- | :-- | :-- | :-- | :-- |
| A | Compute 217 × 18 in Calculator | Calculator | `a11y` (`extract → a11y`) | `s8-5053df41` |
| B | Read the title bar of the VS Code window containing 'APP9' | VS Code Insiders | `cdp` (Electron) | `s8-fc30177a` |
| C | Click the brown cookie sprite drawn on a `<canvas>` | Safari | `vision` (`extract → a11y → vision`) | `s8-3ba72fea` |

```bash
cd "/Users/bavyabalakrishnan/EAG V3/APP9/S9SharedCode/code"

# A — Calculator (lands on a11y: clicks digit buttons, reads the display)
uv run python flow.py "Open the Calculator app on Mac and compute 217 multiplied by 18. Tell me the result."

# B — VS Code Insiders title bar (Electron CDP tier; disambiguates by title)
uv run python flow.py "In VS Code Insiders, read the title bar of the window whose title contains 'APP9'."

# C — Cookie Clicker canvas sprite (escalates all the way to vision)
uv run python flow.py "In Safari, open https://orteil.dashnet.org/cookieclicker/ and click the large brown cookie sprite drawn on the canvas. Confirm it was clicked."
```

---

## Results

### A · Calculator — `path = a11y`

```text
session s8-5053df41 ─ query: Open the Calculator app on Mac and compute 217 multiplied by 18. Tell me the result.
[n:1] planner   complete (1.4s)
[n:2] computer  complete (7.4s)   path=a11y   cascade=extract → a11y   turns=1
[n:3] formatter complete (2.2s)

FINAL: The result of 217 multiplied by 18 is 3,906.
```

The goal is interactive ("compute"), so the cascade skips passive `extract`
and the a11y judge clicks the digit buttons (`2`, `1`, `7`, `×`, `1`, `8`, `=`)
off the Calculator AX tree, then reads the display back.

### B · VS Code Insiders — `path = cdp`

```text
session s8-fc30177a ─ query: In VS Code Insiders, read the title bar of the window whose title contains 'APP9'.
[n:1] planner   complete (4.1s)
[n:2] computer  complete (16.1s)  path=cdp   cascade=cdp   turns=2
[n:3] formatter complete (1.6s)

FINAL: The title bar of the VS Code Insiders window containing 'APP9' is: output-computer — APP9.
```

VS Code is Electron, so the cascade goes straight to the `cdp` tier. The LLM
reads a compact DOM snapshot over the Chrome DevTools Protocol and picks the
selector that returns the workbench title. The query **disambiguates by
title** (`contains 'APP9'`) so it targets the right window rather than the
first CDP page target.

### C · Cookie Clicker (canvas) — `path = vision`

```text
session s8-3ba72fea ─ query: In Safari, open …/cookieclicker/ and click the large brown cookie sprite drawn on the canvas. Confirm it was clicked.
[n:1] planner   complete (3.9s)
[n:2] computer  complete (39.4s)  path=vision   cascade=extract → a11y → vision   turns=1
[n:3] formatter complete (4.8s)

FINAL: The large brown cookie sprite on the canvas was successfully clicked in Safari.
       Visual confirmation shows the click landed accurately on the target.
```

The cookie is drawn on a `<canvas>` — it has **no** AX node, so `extract` and
`a11y` both escalate and the cascade lands on `vision`: a screenshot is
annotated with a numbered set-of-marks grid, the V9 vision model picks the
grid cell over the cookie (mark 18), and the driver clicks it.

---

## Trajectory / submission evidence

Every run is wrapped in `start_recording` / `stop_recording`, writing a
turn-numbered trajectory directory — **the submission evidence**. For the
vision run:

```text
state/sessions/s8-3ba72fea/computer/computer_1781619775/
  turn-00001/screenshot.png      window capture before the action
  turn-00002/screenshot.png      window capture after the action
  turn-00002/click.png           the click target
  vision_raw.png                 unannotated screenshot the model saw
  vision_marked.png              the set-of-marks grid the model chose from
  vision_click_debug.png         the chosen mark overlaid on the click point
  vision_verify.png              before/after used to confirm the click
```

The `hotkey`/`a11y`/`cdp` tiers write per-turn screenshots without the vision
overlays (they don't call the vision model).

What to record per run:

- `success`, `path` (the tier that answered), and `cascade` (full attempt order)
- `content` (the extracted answer) and the `actions` list
- the trajectory directory under `state/sessions/<sid>/computer/computer_<ts>/`
- the per-node trace and `FINAL:` answer (also replayable with `replay.py`)

---

## Failure modes encountered

Real failures hit while building and running the three tasks, and how the
cascade / orchestrator handled each.

### 1 · CDP attaches to the wrong window (ambiguous target)

An early Task-B query — *"Read the title bar text of the currently open VS
Code Insiders window"* (`s8-641a5877`) — returned **`config —
ETRules_Python`**, a *different* window than the focused `APP9` one. VS Code
exposes one remote-debugging port shared by all windows; the `cdp` tier grabs
the **first page target**, not the frontmost window, and it does not follow
macOS focus.
**Fix:** disambiguate in the goal (`…window whose title contains 'APP9'`),
which made the CDP judge select the matching target → `s8-fc30177a` returned
`output-computer — APP9`.

### 2 · `launch_app` failure → planner recovery (not a crash)

Some computer nodes failed first with
`cua-driver error: launch_app failed after 3 attempt(s): Provide either
bundle_id` (e.g. `s8-6fd017a6`, `s8-5d147422`). The skill raised
`error_code="interaction_failed"`, `recovery.classify_failure` saw an
`upstream_failure`, and the orchestrator queued a **fresh planner node** that
re-emitted the computer node with a proper `bundle_id` — the second attempt
completed. Graceful-fail-by-replanning, exactly like the Browser skill.

### 3 · Vision click hits the wrong element (state-changing task)

A deliberately harder Task-B variant — *find the APP9 window, read the active
tab, **close it**, and confirm* (`s8-cd7e8161`) — escalated `cdp → vision`
(the tiny tab close-icon was not a clean DOM target). Vision then **clicked a
folder in the Explorer sidebar instead of the editor tab's close button**, so
the tab did not close. The formatter reported this **honestly** ("the attempt
to close it failed because the click incorrectly targeted a folder…") rather
than hallucinating success. Lesson: small, visually-crowded targets are the
weak spot of the vision tier; prefer a command (`workbench.action.closeActive
Editor` via the `cdp` tier) for state-changing actions.

### 4 · Action lands but the app doesn't react (verify caught it)

Task C (`s8-3ba72fea`) clicked the cookie accurately — the before/after
`vision_verify.png` confirmed the click landed on the sprite — but the game's
cookie counter did **not** advance. The verify step surfaced the discrepancy,
so the final answer was qualified ("the click landed accurately… the counter
did not increase") instead of overclaiming. A landed click ≠ a triggered app
behaviour; always verify post-state, not just the click coordinate.

### 5 · Empty AX tree (daemon identity / canvas / Electron)

A scan returning `element_count == 0` raises a typed `PreconditionError`
naming every candidate cause: TCC grants missing, window not activated, an
Electron opaque `AXWebArea` (needs `cdp`), or a `<canvas>` surface (needs
`vision`). The most common operational trap is **daemon identity**: a
cua-driver daemon launched from the terminal runs under the terminal's
*ungranted* identity and silently returns empty trees. `ensure_daemon()`
always (re)launches the granted `com.trycua.driver` app bundle
(`open -n -g -a CuaDriver --args serve`) and verifies Accessibility + Screen
Recording grants before any tier runs.

---

## Cost summary (per agent)

All three runs are free-tier Gemini Flash-Lite; `dollars = 0.0` for every call.

| Session | planner | computer | formatter | computer tier |
| :-- | :-- | :-- | :-- | :-- |
| A `s8-5053df41` | 1 call | 1 call | 1 call | a11y (1 LLM judge) |
| B `s8-fc30177a` | 1 call | 1 call | 1 call | cdp (1 LLM judge) |
| C `s8-3ba72fea` | 1 call | 3 calls | 1 call | vision (3 multimodal turns) |

The `computer` agent is a separately-attributable line on
`/v1/cost/by_agent` — vision is the only tier that drives token count up; the
a11y and cdp tiers are a single cheap text-LLM judgment each, and `extract` /
`hotkey` are $0 (no LLM at all).

---

## Architecture note

The agent is a **growing-DAG orchestrator**: a planner emits a small graph of
typed skill nodes, the executor walks ready nodes, and a skill that fails a
critic check spawns a fresh planner node that grows the graph with a corrected
plan. `flow.py` is never modified — Computer behaviour plugs in through the
skill catalogue, exactly like Browser.

The Computer skill mirrors the Browser cost cascade, retargeted to the
desktop:

- **`extract`** — read-only AX text / clipboard. Used only for READ goals with
  text already present; an interactive goal always escalates.
- **`hotkey`** — a deterministic key sequence (the sequence *is* the policy),
  verified by copying the result to the clipboard and reading it back.
- **`a11y`** — a cheap text-LLM reads the AX tree (each actionable node tagged
  `[element_index N]`) and returns `answer` / `act` / `escalate`.
- **`cdp`** — for Electron apps (VS Code), the LLM reads a compact DOM snapshot
  over CDP and chooses a CSS selector + action, or escalates.
- **`vision`** — a multimodal set-of-marks call locates targets that are
  *visually drawn* (canvas sprites, images) with no AX/DOM node.

The daemon must run under the **granted app-bundle identity**
(`com.trycua.driver`), launched via `open -n -g -a CuaDriver --args serve`;
`ensure_daemon()` guarantees this and verifies the Accessibility + Screen
Recording TCC grants before any tier runs. Every LLM call routes through the
**V9 gateway** (`:8109`), which tags calls with `agent=computer` so per-agent
token usage and dollar cost surface on `/v1/cost/by_agent`. Each run is
persisted under `state/sessions/<sid>/` (`query.txt`, `graph.json`,
`nodes/*.json`, and per-turn trajectory artifacts) — exactly what `replay.py`
reads back node-by-node.

---

## Reproduce

```bash
# 1. start the V9 gateway (port 8109)
cd "/Users/bavyabalakrishnan/EAG V3/APP9/llm_gatewayV9" && uv run python main.py

# 2. ensure the cua-driver app-bundle daemon is granted (one-time)
~/.local/bin/cua-driver permissions grant   # enable Accessibility + Screen Recording
#    (the skill auto-launches it via `open -n -g -a CuaDriver --args serve`)

# 3. run the three assignment queries
cd "/Users/bavyabalakrishnan/EAG V3/APP9/S9SharedCode/code"
uv run python flow.py "Open the Calculator app on Mac and compute 217 multiplied by 18. Tell me the result."
uv run python flow.py "In VS Code Insiders, read the title bar of the window whose title contains 'APP9'."
uv run python flow.py "In Safari, open https://orteil.dashnet.org/cookieclicker/ and click the large brown cookie sprite drawn on the canvas. Confirm it was clicked."

# 4. replay any run (enter = next, p = prompt, o = output, q = quit)
uv run python replay.py s8-5053df41

# 5. cost summary
curl -s "http://localhost:8109/v1/cost/by_agent?session=s8-5053df41" | python3 -m json.tool
```

Standalone runner (bypasses the orchestrator to exercise each tier directly
and write a trajectory):

```bash
uv run python -m computer.run_tasks calc     # Calculator — deterministic hotkey tier
uv run python -m computer.run_tasks maps     # canvas in Safari — falls through to vision
uv run python -m computer.run_tasks vscode   # VS Code — Electron CDP tier
uv run python -m computer.run_tasks all
```

---

*The previous Browser-skill README is preserved at
[README.session9-browser.md](README.session9-browser.md).*
