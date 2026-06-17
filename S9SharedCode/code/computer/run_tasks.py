"""Standalone runner for the three Computer-Use assignment tasks.

Runs each task directly through ComputerSkill (bypassing the full graph
orchestrator) so the cua-driver cascade can be exercised and recorded on
its own. Every run writes a cua-driver trajectory directory — the
submission evidence.

Usage:
    python -m computer.run_tasks calc      # Task A — cascade stops at hotkey (zero vision)
    python -m computer.run_tasks maps      # Task B — cascade falls through to vision (Google Maps in Safari)
    python -m computer.run_tasks vscode    # Task C — cascade uses CDP (Electron)
    python -m computer.run_tasks all

No task pins a layer: each demonstrates the natural Browser-style cascade.
The tier that actually answered is surfaced as output.path and the full
attempt order as output.cascade. (Set metadata.force_path to pin a tier.)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from schemas import NodeSpec
from computer.skill import ComputerSkill

ARTIFACTS = Path(__file__).resolve().parent.parent / "state" / "sessions" / "computer-demo"


# ── task definitions (metadata only — all safe, stateless targets) ───────────
def task_calc() -> NodeSpec:
    """Compute 7 × 8 in Calculator. The cascade tries extract (escalates: the
    goal is interactive), then hotkey — a deterministic key sequence that
    verifies via the clipboard and stops. Zero vision, zero LLM in the loop."""
    return NodeSpec(skill="computer", inputs=[], metadata={
        "app": "Calculator",
        "bundle_id": "com.apple.calculator",
        "goal": "compute 7 x 8 and read the result",
        # Esc clears; '*' is shift+8 (symbols aren't key names); '=' is return.
        "keys": ["escape", "7", {"key": "8", "modifiers": ["shift"]}, "8", "return"],
        "expect": "56",
    })


def task_maps() -> NodeSpec:
    """Click the big cookie in Cookie Clicker (Safari). The cookie is rendered
    on a <canvas> element — the AX tree has zero content for it (only toolbar
    buttons and score text). The cascade tries extract (escalates — goal is a
    click on a canvas image), skips hotkey (no keys), tries a11y (escalates —
    the cookie sprite is canvas-drawn, not an AX node), then lands on vision
    which identifies the large round brown cookie and clicks it. No login."""
    return NodeSpec(skill="computer", inputs=[], metadata={
        "app": "Safari",
        "bundle_id": "com.apple.Safari",
        "urls": ["https://orteil.dashnet.org/cookieclicker/"],
        "goal": "A Change Language dialog may be visible — if so, click 'English' to dismiss it; otherwise click the large round brown cookie on the left side of the screen",
        "grid": {"zoom": False},
        "pre_js": "localStorage.clear(); location.reload();",
        "pre_js_settle_s": 8.0,
        "page_settle_s": 8.0,
    })


def task_vscode() -> NodeSpec:
    """Read VS Code Insiders workbench state via the agentic CDP layer.
    The planner supplies only a goal — no hardcoded selectors. The LLM
    inspects the live DOM snapshot and picks the right action + selector,
    mirroring how the a11y layer reads the AX tree."""
    return NodeSpec(skill="computer", inputs=[], metadata={
        "app": "VS Code Insiders",
        "bundle_id": "com.microsoft.VSCodeInsiders",
        "electron_debugging_port": 9222,
        "goal": "read the workbench title bar text",
        "settle_s": 4.0,
    })


TASKS = {"calc": task_calc, "maps": task_maps, "vscode": task_vscode}


async def _run_one(name: str) -> None:
    spec = TASKS[name]()
    sk = ComputerSkill(
        artifacts_root=str(ARTIFACTS / name),
        session="computer-demo",
        # Calculator/VS Code don't need a vision provider; game does — leave
        # the pin None so the gateway picks an eligible vision provider.
    )
    print(f"\n{'='*70}\nTASK {name}  —  {spec.metadata['goal']}\n{'='*70}")
    result = await sk.run(spec)
    print(f"success      = {result.success}")
    print(f"path (layer) = {result.output.get('path')}")
    print(f"cascade      = {' → '.join(result.output.get('cascade', [])) or '(none)'}")
    print(f"content      = {result.output.get('content')}")
    print(f"actions      = {json.dumps(result.output.get('actions', []))[:300]}")
    print(f"trajectory   = {result.output.get('trajectory_dir')}")
    if result.error:
        print(f"error        = {result.error}")


async def _main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "calc"
    names = list(TASKS) if which == "all" else [which]
    for n in names:
        if n not in TASKS:
            print(f"unknown task {n!r}; choose from {list(TASKS)} or 'all'")
            continue
        await _run_one(n)


if __name__ == "__main__":
    asyncio.run(_main())
