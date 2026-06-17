"""Session 9 — the Computer-Use skill.

Desktop twin of the Browser skill. Where Browser drives a web page through
extract → deterministic → a11y → vision, Computer drives a *desktop app*
through cua-driver across the same cascade, retargeted from DOM to the OS
accessibility tree:

    Layer 1   extract — read-only AX text / clipboard (no click, no LLM). $0
    Layer 2a  hotkey  — deterministic key sequences (Calculator).        $0
    Layer 2b  a11y    — get_window_state tree + cheap LLM judgment.       ¢
    Layer 2*  cdp     — Electron page tool over CDP (VS Code).            ¢
    Layer 3   vision  — screenshot + set-of-marks + V9 /v1/vision.        $$

Like the Browser skill, the layers run as a NATURAL CASCADE: each tier is
attempted in order and escalates to the next only when it cannot satisfy
the goal (empty / insufficient output). The skill stops at the first tier
that succeeds. `metadata.force_path` (or `layer`) pins a single tier as an
escape hatch. `output.path` is the tier that produced the answer and
`output.cascade` lists every tier attempted — exactly like BrowserOutput,
so replay and the cost ledger render them the same way.

Every run is wrapped in start_recording / stop_recording — the resulting
turn-numbered trajectory directory is the assignment's submission evidence.

SAFETY: targets are stateless system apps (Calculator), public no-login
pages (a browser game), or throwaway scratch windows. The skill never
opens the user's documents, projects, or authenticated sessions.
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from schemas import AgentResult, ComputerOutput, NodeSpec

from . import cua
from .marks import annotate_grid, legend, to_data_url

# V9 client is reused verbatim from the Browser skill — same gateway,
# same /v1/vision + /v1/chat surface, same ledger attribution.
from browser.client import V9Client


# Vision action schema — the model returns a single mark to click plus a
# done flag, mirroring the Browser SoM driver's structured-action contract.
_VISION_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mark", "reason"],
    "properties": {
        "mark":   {"type": "integer", "description": "grid mark number to click"},
        "reason": {"type": "string", "description": "what is at that mark"},
        "done":   {"type": "boolean"},
    },
}

_VISION_SYSTEM = (
    "You are a desktop-driving agent looking at a screenshot of a single "
    "application window. A numbered grid is drawn over it; each number sits "
    "at the centre of its cell. Pick the SINGLE grid number whose cell best "
    "covers the target described in the goal. Return that number as `mark` "
    "and a short `reason` naming what is at that cell. Do not guess raw "
    "pixel coordinates — only choose a grid number."
)

# Layer-2b judge schema — the cheap text LLM reads the AX tree + goal and
# emits ONE verdict: answer (read goal satisfied), act (click an
# element_index), or escalate (target absent — fall through to vision).
_A11Y_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "reason"],
    "properties": {
        "verdict":       {"type": "string", "enum": ["answer", "act", "escalate"]},
        "element_index": {"type": "integer"},
        "answer":        {"type": "string"},
        "reason":        {"type": "string"},
    },
}

_A11Y_SYSTEM = (
    "You drive a desktop app through its accessibility tree. Each actionable "
    "element is tagged [element_index N]. Given the tree and a goal, choose "
    "exactly one verdict:\n"
    "- 'answer' with the text, if the goal is a READ and the answer is present "
    "in the tree.\n"
    "- 'act' with an element_index, ONLY when that element directly and "
    "unambiguously IS the target the goal names (same control, not merely a "
    "word-overlap). A navigation link or menu item that happens to share a "
    "word with the goal is NOT a match.\n"
    "- 'escalate' when the real target is something VISUALLY DRAWN rather than "
    "a real control — a game character/sprite, a shape, an image, anything on "
    "a <canvas> or opaque web area. Such targets never appear as a tagged "
    "element, so do not substitute a loosely-related link or button for them.\n"
    "When in doubt between 'act' on a weakly-related element and 'escalate', "
    "choose 'escalate' — vision will locate the drawn target."
)

# Layer-2* CDP judge schema — the LLM reads a compact DOM snapshot and
# decides which CSS selector + action satisfies the goal, or escalates.
_CDP_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "reason"],
    "properties": {
        "verdict":      {"type": "string", "enum": ["act", "escalate"]},
        "action":       {"type": "string", "enum": ["get_text", "query_dom",
                                                    "execute_javascript",
                                                    "click_element"]},
        "css_selector": {"type": "string"},
        "javascript":   {"type": "string"},
        "reason":       {"type": "string"},
    },
}

_CDP_SYSTEM = (
    "You are a desktop-driving agent with access to an Electron app's DOM "
    "via Chrome DevTools Protocol. You receive a compact DOM snapshot "
    "(tag + class + text, depth-limited) and a goal. Choose exactly one verdict:\n"
    "- 'act': pick the CDP action and CSS selector (or JS) that best satisfies "
    "the goal. Actions: get_text (read text content), query_dom (structured "
    "element info), execute_javascript (run JS expression, use for computed "
    "state), click_element (click by selector).\n"
    "- 'escalate': the goal cannot be satisfied from the DOM (e.g. needs "
    "pixel-level vision).\n"
    "Prefer the most specific selector that uniquely targets the goal element. "
    "For read goals prefer get_text; for computed/dynamic state prefer "
    "execute_javascript. Be terse in reason."
)


class ComputerSkill:
    NAME = "computer"

    def __init__(self, *, gateway_url: str = "http://localhost:8109",
                 agent_tag: str = "computer",
                 a11y_provider_pin: str | None = "gemini",
                 vision_provider_pin: str | None = None,
                 artifacts_root: str | None = None,
                 session: str | None = None,
                 record_video: bool = False):
        self.gateway_url = gateway_url
        self.agent_tag = agent_tag
        self.a11y_provider_pin = a11y_provider_pin
        self.vision_provider_pin = vision_provider_pin
        self.artifacts_root = Path(artifacts_root) if artifacts_root else None
        self.session = session
        self.record_video = record_video

    # ── public entry point ──────────────────────────────────────────────────
    async def run(self, node: NodeSpec) -> AgentResult:
        md = node.metadata or {}
        goal = md.get("goal") or "complete the task"
        app_name = md.get("app") or md.get("name") or md.get("bundle_id") or "app"
        # force_path / layer pins a single tier (escape hatch + back-compat);
        # absence runs the natural cost cascade, exactly like the Browser skill.
        force = (str(md.get("force_path") or md.get("layer") or "")).lower() or None
        valid = ("extract", "hotkey", "a11y", "cdp", "vision")
        if force and force not in valid:
            return self._error(app_name, goal, "interaction_failed",
                               f"force_path/layer must be one of {valid}, got {force!r}")
        t0 = time.time()
        is_electron = (bool(md.get("electron_debugging_port"))
                       or _is_electron_bundle(md.get("bundle_id")))

        # Screen Recording is only required if vision can actually run.
        vision_reachable = force in (None, "vision")
        try:
            await asyncio.to_thread(cua.ensure_daemon,
                                    require_screen_recording=vision_reachable)
        except cua.PermissionsError as e:
            return self._error(app_name, goal, "interaction_failed", str(e),
                               elapsed=time.time() - t0)

        # Recording = the submission evidence. try/finally so the trajectory
        # survives even if the run raises.
        rec_dir = None
        if self.artifacts_root:
            rec_dir = str(self.artifacts_root / f"computer_{int(t0)}")
            try:
                await asyncio.to_thread(cua.start_recording, rec_dir,
                                        record_video=self.record_video)
            except cua.CuaError:
                rec_dir = None  # best-effort; never fail the task on recording

        last: ComputerOutput | None = None
        final_escalate = True
        cascade: list[str] = []
        current_layer = "extract"
        try:
            # Launch ONCE; every layer drives the same window.
            pid, wid = await self._launch(
                md, app_name, is_electron=is_electron,
                activate_settle=2.0 if vision_reachable else 1.0)
            # The cascade: cheapest applicable tier first, escalate on failure.
            order = [force] if force else (
                ["cdp", "vision"] if is_electron
                else ["extract", "hotkey", "a11y", "vision"])
            current_layer = order[0]
            for layer in order:
                current_layer = layer
                res = await self._run_layer(
                    layer, pid, wid, md, app_name, goal, rec_dir)
                if res is None:          # tier not applicable (e.g. hotkey w/o keys)
                    continue
                out, escalate = res
                cascade.append(layer)
                last, final_escalate = out, escalate
                if force or not escalate:
                    break
        except cua.PreconditionError as e:
            return self._error(app_name, goal, "interaction_failed",
                               f"precondition failed: {e}", path=current_layer,
                               elapsed=time.time() - t0)
        except cua.CuaError as e:
            return self._error(app_name, goal, "interaction_failed",
                               f"cua-driver error: {e}", path=current_layer,
                               elapsed=time.time() - t0)
        finally:
            if rec_dir:
                try:
                    await asyncio.to_thread(cua.stop_recording)
                except cua.CuaError:
                    pass

        if last is None:
            return self._error(
                app_name, goal, "interaction_failed",
                f"no applicable layer ran (cascade={cascade or 'none'})",
                elapsed=time.time() - t0)
        last.trajectory_dir = rec_dir
        last.cascade = cascade
        success = (out_success(last) if force
                   else (out_success(last) and not final_escalate))
        return AgentResult(success=success, agent_name=self.NAME,
                           output=last.model_dump(), elapsed_s=time.time() - t0)

    # ── cascade dispatch ────────────────────────────────────────────────────
    async def _run_layer(self, layer: str, pid: int, wid: int | None, md: dict,
                         app_name: str, goal: str, rec_dir: str | None):
        """Return (ComputerOutput, escalate) for the tier, or None if the tier
        does not apply to this node (so the cascade simply skips it)."""
        if layer == "extract":
            return await self._try_extract(pid, wid, md, app_name, goal)
        if layer == "hotkey":
            if not md.get("keys"):
                return None
            return await self._run_hotkey(pid, wid, md, app_name, goal)
        if layer == "cdp":
            return await self._run_cdp(pid, wid, md, app_name, goal)
        if layer == "a11y":
            return await self._run_a11y(pid, wid, md, app_name, goal)
        if layer == "vision":
            return await self._run_vision(pid, wid, md, app_name, goal, rec_dir)
        return None

    # ── Layer 1: extract (read-only AX text, $0, no LLM) ─────────────────────
    async def _try_extract(self, pid: int, wid: int | None, md: dict,
                           app_name: str, goal: str) -> tuple[ComputerOutput, bool]:
        """Read state straight off the AX tree — no click, no LLM. Useful only
        when the goal is a READ and substantial text is present. An interactive
        goal (click / compute / type / …) always escalates: extraction can't
        act on the world."""
        if wid is None or _is_interactive_goal(goal):
            return self._ph(app_name, goal, "extract", "extraction insufficient"), True
        try:
            state = await asyncio.to_thread(cua.scan, pid, wid, mode="ax",
                                            query=md.get("query"), guard=False)
        except cua.CuaError:
            return self._ph(app_name, goal, "extract", "scan failed"), True
        text = (state.get("tree_markdown") or "").strip()
        if not text:
            return self._ph(app_name, goal, "extract", "no useful text"), True
        # Ask the same LLM judge as a11y: does this tree actually answer the goal?
        client = V9Client(base_url=self.gateway_url, agent=self.agent_tag,
                          session=self.session)
        prompt = (f"GOAL: {goal}\n\nACCESSIBILITY TREE:\n{text[:6000]}\n\n"
                  f"Decide the next action.")
        res = await client.chat(prompt, system=_A11Y_SYSTEM, schema=_A11Y_SCHEMA,
                                schema_name="A11yAct", max_tokens=400,
                                provider=self.a11y_provider_pin)
        p = res.parsed or {}
        verdict = str(p.get("verdict", "")).lower()
        if verdict == "answer" and p.get("answer"):
            return ComputerOutput(app=app_name, goal=goal, path="extract",
                                  turns=1, content=p["answer"]), False
        return self._ph(app_name, goal, "extract", p.get("reason", "escalated")), True

    # ── Layer 2a: deterministic hotkeys (Calculator) ────────────────────────
    async def _run_hotkey(self, pid: int, wid: int | None, md: dict,
                          app_name: str, goal: str) -> tuple[ComputerOutput, bool]:
        """A fixed key sequence — no LLM in the loop. The sequence is the
        policy. We VERIFY by reading the result back: Cmd+C copies the
        Calculator display to the clipboard (a $0 Layer-1 extract), which is
        far more reliable than walking a menu-dominated AX tree. A failed
        verify escalates the cascade."""
        seq = md.get("keys") or _default_calc_sequence()
        actions: list[dict] = []
        for item in seq:
            key, mods = _parse_key(item)
            if mods:
                await asyncio.to_thread(cua.hotkey, pid, mods + [key])
            else:
                await asyncio.to_thread(cua.press_key, pid, key)
            actions.append({"press": key, "modifiers": mods})
            await asyncio.sleep(0.15)

        # VERIFY (Layer-1 extract): copy the display to the clipboard and read
        # it back. Deterministic; no LLM, no fragile tree parsing.
        result_text = None
        if md.get("verify", True):
            await asyncio.to_thread(cua.copy_selection, pid)
            result_text = await asyncio.to_thread(cua.read_clipboard)
        expected = md.get("expect")
        ok = (str(expected) == result_text) if expected is not None else bool(result_text)
        out = ComputerOutput(
            app=app_name, goal=goal, path="hotkey",
            turns=len(actions),
            actions=actions + [{"verify_clipboard": result_text,
                                "expected": expected, "match": ok}],
            content=(f"result={result_text}" if result_text else None),
        )
        return out, (not ok)


    # ── Layer 2b: a11y tree + cheap LLM judgment ────────────────────────────
    async def _run_a11y(self, pid: int, wid: int | None, md: dict,
                        app_name: str, goal: str) -> tuple[ComputerOutput, bool]:
        """Scan the AX tree, then let a cheap text LLM emit one verdict:
        `answer` (read goal satisfied), `act` (click an element_index), or
        `escalate` (target absent — e.g. a canvas / opaque web area). Escalate
        falls the cascade through to vision."""
        if wid is None:
            return self._ph(app_name, goal, "a11y", "no window for AX scan"), True
        try:
            state = await asyncio.to_thread(cua.scan, pid, wid, mode="ax",
                                            query=md.get("query"))
        except cua.PreconditionError:
            return self._ph(app_name, goal, "a11y", "empty AX tree"), True
        tree = (state.get("tree_markdown") or "")[:8000]
        client = V9Client(base_url=self.gateway_url, agent=self.agent_tag,
                          session=self.session)
        prompt = (f"GOAL: {goal}\n\nACCESSIBILITY TREE (actionable elements are "
                  f"tagged [element_index N]):\n{tree}\n\nDecide the next action.")
        res = await client.chat(prompt, system=_A11Y_SYSTEM, schema=_A11Y_SCHEMA,
                                schema_name="A11yAct", max_tokens=400,
                                provider=self.a11y_provider_pin)
        p = res.parsed or {}
        verdict = str(p.get("verdict", "")).lower()
        if verdict == "answer" and p.get("answer"):
            return ComputerOutput(app=app_name, goal=goal, path="a11y", turns=1,
                                  actions=[{"verdict": p}], content=p["answer"]), False
        if verdict == "act" and isinstance(p.get("element_index"), int):
            idx = p["element_index"]
            await asyncio.to_thread(cua.click, pid, wid, element_index=idx)
            return ComputerOutput(
                app=app_name, goal=goal, path="a11y", turns=1,
                actions=[{"act": idx, "reason": p.get("reason", "")}],
                content=f"clicked element_index {idx} — {p.get('reason','')}"), False
        # escalate: target not addressable through the AX tree
        return ComputerOutput(app=app_name, goal=goal, path="a11y", turns=1,
                              actions=[{"verdict": p}], content=None), True


    # ── Layer 2*: Electron CDP page tool — agentic, mirrors a11y ──────────
    async def _run_cdp(self, pid: int, wid: int | None, md: dict, app_name: str,
                       goal: str) -> tuple[ComputerOutput, bool]:
        """Drive an Electron app's DOM via CDP — genuinely agentic, exactly
        like _run_a11y mirrors the browser A11yDriver.

        Flow (mirrors browser A11yDriver._decide):
          1. query_dom on 'body' → compact DOM snapshot (tag+class+text)
          2. V9Client.chat(goal + DOM snapshot) → LLM picks action+selector
          3. Execute that action via cua.page
          4. Empty result or 'escalate' verdict → cascade to vision

        The planner only needs to supply `goal`. It does NOT need to know
        CSS selectors, actions, or JS — the LLM reads the live DOM and
        decides, exactly as the a11y judge reads the AX tree.
        """
        await asyncio.sleep(float(md.get("settle_s", 3.0)))
        if wid is None:
            wid = await asyncio.to_thread(cua.first_window_id, {}, pid)

        # ── step 1: get a compact DOM snapshot (the "legend" equivalent) ────
        try:
            dom_resp = await asyncio.to_thread(
                cua.page, pid, "query_dom",
                css_selector="body", window_id=wid,
            )
        except cua.CuaError as e:
            return self._ph(app_name, goal, "cdp", f"DOM query failed: {e}"), True

        dom_snapshot = _stringify_page(dom_resp)[:6000]
        if not dom_snapshot:
            return self._ph(app_name, goal, "cdp", "empty DOM snapshot"), True

        # ── step 2: LLM judges which action+selector satisfies the goal ─────
        client = V9Client(base_url=self.gateway_url, agent=self.agent_tag,
                          session=self.session)
        prompt = (
            f"GOAL: {goal}\n\n"
            f"DOM SNAPSHOT (tag + class + text, depth-limited):\n{dom_snapshot}\n\n"
            f"Which CDP action and selector best satisfies the goal?"
        )
        res = await client.chat(
            prompt, system=_CDP_SYSTEM, schema=_CDP_SCHEMA,
            schema_name="CDPAct", max_tokens=400,
            provider=self.a11y_provider_pin,
        )
        p = res.parsed or {}
        verdict = str(p.get("verdict", "")).lower()

        if verdict == "escalate":
            return ComputerOutput(
                app=app_name, goal=goal, path="cdp", turns=1,
                actions=[{"verdict": p}], content=None,
            ), True

        # ── step 3: execute the chosen action ───────────────────────────────
        action = p.get("action", "get_text")
        css = p.get("css_selector")
        js = p.get("javascript")
        try:
            exec_resp = await asyncio.to_thread(
                cua.page, pid, action,
                css_selector=css, javascript=js, window_id=wid,
            )
        except cua.CuaError as e:
            return self._ph(app_name, goal, "cdp", f"CDP action failed: {e}"), True

        content = _stringify_page(exec_resp)
        out = ComputerOutput(
            app=app_name, goal=goal, path="cdp", turns=2,
            actions=[
                {"step": "dom_snapshot", "selector": "body"},
                {"step": "execute", "action": action,
                 "css_selector": css, "javascript": js,
                 "reason": p.get("reason", "")},
            ],
            content=content[:4000] if content else None,
        )
        return out, (not bool(content))


    # ── Layer 3: vision (set-of-marks) ──────────────────────────────────────
    async def _run_vision(self, pid: int, wid: int | None, md: dict,
                          app_name: str, goal: str,
                          rec_dir: str | None) -> tuple[ComputerOutput, bool]:
        """screenshot → coarse grid → /v1/vision → native ZOOM → fine grid → click.

        Two-stage set-of-marks: stage 1 picks the coarse cell over the target
        (turns "predict pixels" into "pick a label"); stage 2 uses the driver's
        native `zoom` to crop that cell and re-picks on a fine grid, then clicks
        with `from_zoom=True` so the driver translates the zoom-image pixel back
        to full-window space — no manual coordinate math. `debug_image_out`
        writes an authoritative crosshair screenshot for verification. A canvas /
        game surface is AX-blind, so this is the honest last resort; an unusable
        stage-1 mark escalates."""
        if wid is None:
            return self._ph(app_name, goal, "vision", "no window for screenshot"), True
        client = V9Client(base_url=self.gateway_url, agent=self.agent_tag,
                          session=self.session)
        grid = md.get("grid") or {}
        cols = int(grid.get("cols", 8))
        rows = int(grid.get("rows", 6))
        zcols = int(grid.get("zoom_cols", 6))
        zrows = int(grid.get("zoom_rows", 6))
        zoom_enabled = bool(grid.get("zoom", True))

        # Wait for the page/canvas to actually render before capturing — a
        # blank window screenshot makes the vision call meaningless.
        await asyncio.sleep(float(md.get("page_settle_s", 5.0)))

        # Optional: run JavaScript before the screenshot (e.g. clear localStorage
        # so a game starts fresh). Triggers a reload and waits for re-settle.
        if md.get("pre_js"):
            try:
                await asyncio.to_thread(
                    cua.page, pid, "execute_javascript",
                    javascript=md["pre_js"], window_id=wid,
                )
                await asyncio.sleep(float(md.get("pre_js_settle_s", 5.0)))
            except Exception:
                pass  # non-fatal — continue with whatever state the page is in

        shot_dir = Path(rec_dir) if rec_dir else (self.artifacts_root or Path("."))
        raw_path = str(Path(shot_dir) / "vision_raw.png")
        await asyncio.to_thread(cua.screenshot, pid, wid, raw_path)
        png = Path(raw_path).read_bytes()
        from PIL import Image as _Image
        with _Image.open(raw_path) as _im:
            shot_w, shot_h = _im.size

        # ── describe mode: read-only — screenshot → LLM answer, no click ────
        if md.get("vision_mode") == "describe":
            describe_prompt = (
                f"Look at this screenshot carefully and answer the following question.\n\n"
                f"QUESTION: {goal}\n\n"
                f"Answer concisely and specifically based only on what you can see."
            )
            desc_result = await client.vision(
                to_data_url(png), describe_prompt,
                max_tokens=600,
                provider=self.vision_provider_pin,
            )
            answer = desc_result.text or str(desc_result.parsed or "(no answer)")
            return ComputerOutput(
                app=app_name, goal=goal, path="vision", turns=1,
                actions=[{"mode": "describe", "screenshot": raw_path}],
                content=answer,
            ), False

        # ── stage 1: coarse grid over the whole window ──────────────────────
        # click(x, y) takes WINDOW-LOCAL SCREENSHOT PIXELS — the exact space
        # these marks live in — so a picked mark is clickable as-is (verified
        # with the driver's own debug_image_out; no dpr/origin conversion).
        marked, mark_xy, _ = annotate_grid(png, cols=cols, rows=rows)
        if rec_dir:
            Path(rec_dir, "vision_marked.png").write_bytes(marked)
        prompt = (
            f"GOAL: {goal}\n\n"
            f"The screenshot is a {cols}x{rows} numbered grid.\n"
            f"GRID MARKS (number → window pixel):\n{legend(mark_xy)}\n\n"
            f"Which single grid number is over the target?"
        )
        result = await client.vision(
            to_data_url(marked), prompt, system=_VISION_SYSTEM,
            schema=_VISION_SCHEMA, schema_name="VisionPick", max_tokens=400,
            provider=self.vision_provider_pin,
        )
        parsed = result.parsed or {}
        mark = parsed.get("mark")
        if mark not in mark_xy:
            return ComputerOutput(
                app=app_name, goal=goal, path="vision", turns=1,
                actions=[{"stage": 1, "vision_verdict": parsed}],
                content=f"vision returned unusable mark={mark!r}: {parsed.get('reason', '')}",
            ), True

        px, py = mark_xy[int(mark)]
        actions: list[dict] = [
            {"stage": 1, "vision_mark": mark, "coarse_px": [px, py],
             "reason": parsed.get("reason", "")}
        ]
        from_zoom = False  # whether the final click point is in zoom-image space

        # ── stage 2: native zoom into the chosen cell, re-pick on a fine grid ─
        if zoom_enabled:
            cw, ch = shot_w / cols, shot_h / rows
            idx = int(mark) - 1
            c, r = idx % cols, idx // cols
            x1, y1 = int(c * cw), int(r * ch)
            x2, y2 = int((c + 1) * cw), int((r + 1) * ch)
            zjpeg, zw, zh = await asyncio.to_thread(
                cua.zoom, pid, wid, x1, y1, x2, y2)
            if zjpeg:
                # Fine grid over the native zoom image; marks are in ZOOM-image
                # pixels — from_zoom=True lets the driver translate them back.
                zmarked, zmark_xy, _ = annotate_grid(zjpeg, cols=zcols, rows=zrows)
                if rec_dir:
                    Path(rec_dir, "vision_zoom_raw.jpg").write_bytes(zjpeg)
                    Path(rec_dir, "vision_zoom_marked.png").write_bytes(zmarked)
                zprompt = (
                    f"GOAL: {goal}\n\n"
                    f"This is a ZOOMED-IN crop around the target, shown as a "
                    f"{zcols}x{zrows} numbered grid.\n"
                    f"GRID MARKS (number → image pixel):\n{legend(zmark_xy)}\n\n"
                    f"Pick the single grid number centred most precisely on the target."
                )
                zresult = await client.vision(
                    to_data_url(zmarked), zprompt, system=_VISION_SYSTEM,
                    schema=_VISION_SCHEMA, schema_name="VisionPick", max_tokens=400,
                    provider=self.vision_provider_pin,
                )
                zparsed = zresult.parsed or {}
                zmark = zparsed.get("mark")
                if zmark in zmark_xy:
                    px, py = zmark_xy[int(zmark)]
                    from_zoom = True
                    actions.append({"stage": 2, "vision_mark": zmark,
                                    "fine_zoom_px": [px, py],
                                    "reason": zparsed.get("reason", "")})
                else:
                    # stage-2 miss: keep the coarse full-window pick.
                    actions.append({"stage": 2, "vision_verdict": zparsed,
                                    "note": "unusable fine mark; using coarse centre"})
            else:
                actions.append({"stage": 2, "note": "zoom returned no image; "
                                "using coarse centre"})

        await asyncio.to_thread(cua.click, pid, wid, x=px, y=py,
                                from_zoom=from_zoom)
        # Poll for screen change: take screenshots every 0.5s until the screen
        # differs from the before-click image, or 5s timeout.
        from PIL import Image as _CImg, ImageDraw as _CDraw
        import numpy as _np
        before_arr = _np.array(_CImg.open(raw_path).convert("RGB"), dtype=_np.float32)
        dbg = str(Path(rec_dir) / "vision_click_debug.png") if rec_dir else None
        _poll_tmp = str(Path(rec_dir) / "_poll_tmp.png") if rec_dir else None
        waited = 0.0
        changed = False
        while waited < 5.0:
            await asyncio.sleep(0.5)
            waited += 0.5
            if _poll_tmp:
                await asyncio.to_thread(cua.screenshot, pid, wid, _poll_tmp)
                after_arr = _np.array(_CImg.open(_poll_tmp).convert("RGB"),
                                      dtype=_np.float32)
                diff = float(_np.mean(_np.abs(after_arr - before_arr)))
                if diff > 1.5:   # >1.5 mean pixel change = something visibly changed
                    changed = True
                    break
        # Use the last poll screenshot as the debug image.
        if dbg and _poll_tmp and Path(_poll_tmp).exists():
            import shutil as _sh
            _sh.copy(_poll_tmp, dbg)
        elif dbg:
            await asyncio.to_thread(cua.screenshot, pid, wid, dbg)
        # Draw red crosshair at the actual click coordinates for verification.
        if dbg and Path(dbg).exists():
            _cim = _CImg.open(dbg).convert("RGB")
            _cd = _CDraw.Draw(_cim)
            _cx, _cy, _r = px, py, 20
            _cd.line([(_cx - _r, _cy), (_cx + _r, _cy)], fill=(255, 0, 0), width=3)
            _cd.line([(_cx, _cy - _r), (_cx, _cy + _r)], fill=(255, 0, 0), width=3)
            _cd.ellipse([(_cx - _r, _cy - _r), (_cx + _r, _cy + _r)],
                        outline=(255, 0, 0), width=2)
            _cim.save(dbg)
        actions.append({"click": [px, py], "from_zoom": from_zoom,
                        "debug_image": dbg, "screen_changed": changed,
                        "waited_s": round(waited, 1)})

        # ── post-click verification: stitch before+after, ask LLM what changed ─
        verify_result = "no post-click screenshot"
        if dbg and Path(dbg).exists():
            from PIL import Image as _PImage, ImageDraw as _IDraw
            before = _PImage.open(raw_path).convert("RGB")
            after  = _PImage.open(dbg).convert("RGB")
            bw, bh = before.size
            aw, ah = after.size
            # side-by-side with a 4px divider
            combined = _PImage.new("RGB", (bw + 4 + aw, max(bh, ah)), (80, 80, 80))
            combined.paste(before, (0, 0))
            combined.paste(after,  (bw + 4, 0))
            d = _IDraw.Draw(combined)
            d.text((4, 4),    "BEFORE", fill=(255, 255, 0))
            d.text((bw + 8, 4), "AFTER",  fill=(255, 255, 0))
            import io as _io
            buf = _io.BytesIO()
            combined.save(buf, format="PNG")
            combined_bytes = buf.getvalue()
            cmp_path = str(Path(rec_dir) / "vision_verify.png") if rec_dir else None
            if cmp_path:
                Path(cmp_path).write_bytes(combined_bytes)
            verify_prompt = (
                f"ORIGINAL GOAL: {goal}\n\n"
                f"Left = BEFORE click. Right = AFTER click.\n"
                f"Compare the two halves and describe exactly what changed "
                f"(animation, glow, counter, menu opened, highlight, etc.). "
                f"Then give a one-sentence verdict: did the click land on the "
                f"correct target?"
            )
            vr = await client.vision(
                to_data_url(combined_bytes), verify_prompt,
                max_tokens=250,
                provider=self.vision_provider_pin,
            )
            verify_result = vr.text or str(vr.parsed or "(no answer)")
            actions.append({"verify": verify_result, "verify_image": cmp_path})

        turns = 2 if from_zoom else 1
        return ComputerOutput(
            app=app_name, goal=goal, path="vision", turns=turns,
            actions=actions,
            content=f"clicked target — {parsed.get('reason','')} | verify: {verify_result}",
        ), False

    # ── shared: launch + activate (native or Electron) ──────────────────────
    async def _launch(self, md: dict, app_name: str, *, is_electron: bool,
                      activate_settle: float = 1.0) -> tuple[int, int | None]:
        """Launch the target once and realise its window. A native target needs
        a window for AX/vision; a pure-CDP Electron target can proceed with
        wid=None (the page tool addresses by pid)."""
        port = int(md.get("electron_debugging_port", 9222)) if is_electron else None
        launch_resp = await asyncio.to_thread(
            cua.launch, bundle_id=md.get("bundle_id"), name=md.get("name"),
            urls=md.get("urls"), electron_debugging_port=port,
        )
        pid = launch_resp.get("pid")
        if not pid:
            raise cua.CuaError(f"launch returned no pid for {app_name}")
        # macOS background-launch fix: realise the window in the AX tree.
        await asyncio.to_thread(cua.activate, app_name, activate_settle)
        wid = cua.first_window_id(launch_resp, pid,
                                  title_hint=md.get("window_title_hint"))
        if wid is None and not is_electron:
            raise cua.PreconditionError(
                f"no window found for {app_name} (pid={pid}) after activation")
        return pid, wid

    # ── tiny placeholder output for escalating tiers ────────────────────────
    def _ph(self, app: str, goal: str, path: str, note: str) -> ComputerOutput:
        return ComputerOutput(app=app, goal=goal, path=path, turns=0, content=note)

    # ── packers ─────────────────────────────────────────────────────────────
    def _error(self, app: str, goal: str, code: str, msg: str,
               *, path: str = "extract", elapsed: float = 0.0) -> AgentResult:
        out = ComputerOutput(app=app, goal=goal, path=path, turns=0, content=None)
        return AgentResult(success=False, agent_name=self.NAME,
                           output=out.model_dump(), error=msg,
                           error_code=code, elapsed_s=elapsed)


# ── helpers ──────────────────────────────────────────────────────────────────
def out_success(out: ComputerOutput) -> bool:
    """A run is successful when its layer produced usable content / action."""
    return bool(out.content) or bool(out.actions)


# Known Electron bundles → the cascade prefers CDP over an opaque AX tree.
# (Targets that pass electron_debugging_port are also treated as Electron.)
_ELECTRON_BUNDLES = {
    "com.microsoft.vscode",
    "com.microsoft.vscodeinsiders",
    "com.microsoft.vscode-insiders",
    "com.tinyspeck.slackmacgap",
    "com.hnc.discord",
    "notion.id",
    "md.obsidian",
}


def _is_electron_bundle(bundle_id: str | None) -> bool:
    return bool(bundle_id) and bundle_id.lower() in _ELECTRON_BUNDLES


# Verbs that imply the goal needs an ACTION on the world, so a read-only
# Layer-1 extract cannot satisfy it and must escalate.
_INTERACTIVE_VERBS = (
    "click", "type", "press", "compute", "calculate", "fill", "select",
    "drag", "open", "run", "new game", "play", "enter", "submit",
    "navigate", "go to", "search",
)


def _is_interactive_goal(goal: str) -> bool:
    g = (goal or "").lower()
    return any(verb in g for verb in _INTERACTIVE_VERBS)


def _parse_key(item: Any) -> tuple[str, list[str]]:
    """Normalise a sequence item into (key, modifiers).

    Accepts:  "7"  |  {"key": "8", "modifiers": ["shift"]}  |  "shift+8"
    """
    if isinstance(item, dict):
        return str(item.get("key", "")), list(item.get("modifiers", []) or [])
    s = str(item)
    if "+" in s and len(s) > 1:
        *mods, key = s.split("+")
        return key, mods
    return s, []


def _default_calc_sequence() -> list[Any]:
    """Compute 7 × 8: '*' is Shift+8 (symbols aren't key names); '=' is Return."""
    return ["7", {"key": "8", "modifiers": ["shift"]}, "8", "return"]


def _read_calc_display(tree_markdown: str) -> str | None:
    """Pull the most likely numeric result off the Calculator AX tree."""
    candidates: list[str] = []
    for line in tree_markdown.splitlines():
        if "AXStaticText" in line or "StaticText" in line:
            m = re.search(r'"([^"]+)"', line)
            if m:
                candidates.append(m.group(1))
    # Prefer a token that contains a digit (the display), longest wins.
    nums = [c for c in candidates if re.search(r"\d", c)]
    if nums:
        return max(nums, key=len).strip()
    return candidates[-1].strip() if candidates else None


def _stringify_page(resp: dict) -> str:
    """page tool replies vary by action (text, dom rows, js result)."""
    for k in ("text", "result", "value", "raw"):
        v = resp.get(k)
        if isinstance(v, str) and v.strip():
            return v
    if resp.get("elements"):
        return "\n".join(str(e) for e in resp["elements"][:50])
    return str(resp)[:4000]
