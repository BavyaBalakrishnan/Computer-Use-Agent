"""Thin, framework-free wrapper around the cua-driver CLI.

Every call shells out to `cua-driver call <tool> <json>` and parses the
JSON reply. The wrapper exists to bake in the hard-won operational
lessons so the skill code above it stays clean:

  - Daemon IDENTITY. cua-driver only walks the AX tree when the daemon
    runs under the *app bundle* identity `com.trycua.driver` (the one
    that holds the TCC grants). A daemon started from the terminal
    symlink runs under the terminal's UNgranted identity and silently
    returns empty trees. `ensure_daemon()` always (re)launches the app
    bundle via LaunchServices and verifies grants before returning.

  - Socket races. Right after a daemon (re)start the Unix socket can
    refuse a connection ("Resource temporarily unavailable", os error
    35) or close without a reply. `call()` retries a few times.

  - The empty-tree guard. A missing grant / un-activated window / canvas
    app all surface as `element_count == 0`. `scan()` raises a typed
    PreconditionError naming every candidate cause, so the caller fails
    loudly instead of addressing a stale cache.

  - The macOS background-launch trap. `launch_app` does not steal focus,
    so a freshly launched app's window is not yet realised in the AX
    hierarchy. `activate()` (AppleScript) + a short settle re-realises it.

No third-party deps — just subprocess + json + the stdlib.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

# ── binary / app-bundle locations ────────────────────────────────────────────
CUA_BIN = str(Path.home() / ".local" / "bin" / "cua-driver")
CUA_APP = "CuaDriver"  # LaunchServices name; the granted com.trycua.driver bundle


def _bin() -> str:
    """Resolve the cua-driver binary: the ~/.local/bin symlink first, then
    anything on PATH. Raises if neither exists."""
    if Path(CUA_BIN).exists():
        return CUA_BIN
    found = shutil.which("cua-driver")
    if found:
        return found
    raise CuaError(
        "cua-driver binary not found. Install it with:\n"
        '  /bin/bash -c "$(curl -fsSL '
        'https://raw.githubusercontent.com/trycua/cua/main/'
        'libs/cua-driver/scripts/install.sh)"'
    )


# ── typed errors ─────────────────────────────────────────────────────────────
class CuaError(RuntimeError):
    """Any cua-driver call that exits non-zero or returns unparseable JSON."""


class PermissionsError(CuaError):
    """TCC grants (Accessibility / Screen Recording) are missing for the
    driver bundle. Terminal: `cua-driver permissions grant` (accept both
    macOS dialogs)."""


class PreconditionError(CuaError):
    """A scan returned an empty AX tree. One of: permissions not granted,
    window not activated, Electron opaque AXWebArea (needs CDP), or a
    canvas/game surface (needs vision)."""


# ── low-level call ───────────────────────────────────────────────────────────
def call(tool: str, payload: dict[str, Any] | None = None,
         *, retries: int = 3, timeout: float = 60.0) -> dict:
    """Invoke one cua-driver tool through the daemon and return parsed JSON.

    Retries on the post-(re)start socket races that surface as a daemon
    proxy warning + in-process fallback. Raises CuaError on a hard failure.
    """
    args = [_bin(), "call", tool, json.dumps(payload or {})]
    last_err = ""
    for attempt in range(1, retries + 1):
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        out, err = proc.stdout.strip(), proc.stderr.strip()
        # The daemon proxy race: the CLI prints a warning and falls back to
        # an in-process run under the (ungranted) terminal identity. Detect
        # it and retry rather than trust the degraded result.
        proxy_race = "daemon proxy" in err or "closed connection" in err \
            or "os error 35" in err
        if proc.returncode == 0 and out and not proxy_race:
            try:
                return json.loads(out) if out.startswith("{") else {"raw": out}
            except json.JSONDecodeError:
                return {"raw": out}
        last_err = err or out or f"exit {proc.returncode}"
        if attempt < retries:
            time.sleep(0.4 * attempt)
    raise CuaError(f"{tool} failed after {retries} attempt(s): {last_err}")


# ── daemon / permissions ─────────────────────────────────────────────────────
def permissions() -> tuple[bool, bool, str]:
    """Return (accessibility_ok, screen_recording_ok, raw_status_text).

    Reads `cua-driver permissions status`. Both flags are only meaningful
    when a daemon under com.trycua.driver is running; otherwise the status
    reports 'unknown' and both come back False."""
    proc = subprocess.run([_bin(), "permissions", "status"],
                          capture_output=True, text=True, timeout=20)
    text = (proc.stdout or "") + (proc.stderr or "")
    acc = "Accessibility:" in text and "granted" in text.split("Accessibility:", 1)[1].split("\n", 1)[0]
    scr = "Screen Recording:" in text and "granted" in text.split("Screen Recording:", 1)[1].split("\n", 1)[0]
    return acc, scr, text.strip()


def ensure_daemon(*, require_screen_recording: bool = True,
                  wait_s: float = 8.0) -> None:
    """Guarantee a *granted* app-bundle daemon is running.

    First check: if grants already read back, we are done. Otherwise launch
    the CuaDriver.app bundle as a backgrounded daemon (`open -n -g -a
    CuaDriver --args serve`) — this is the ONLY launch path that runs under
    the granted com.trycua.driver identity — and poll until the grants
    report or we time out.
    """
    acc, scr, _ = permissions()
    if acc and (scr or not require_screen_recording):
        return
    # (Re)launch the granted app-bundle daemon. `open` returns immediately.
    subprocess.run(["open", "-n", "-g", "-a", CUA_APP, "--args", "serve"],
                   capture_output=True, text=True)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(0.6)
        acc, scr, text = permissions()
        if acc and (scr or not require_screen_recording):
            return
    _, _, text = permissions()
    raise PermissionsError(
        "cua-driver does not have the required TCC grants.\n"
        "Run in your terminal:  ~/.local/bin/cua-driver permissions grant\n"
        "and enable BOTH 'Accessibility' and 'Screen Recording' for "
        "'Cua Driver' in System Settings → Privacy & Security.\n"
        f"--- current status ---\n{text}"
    )


# ── app lifecycle ────────────────────────────────────────────────────────────
def launch(*, bundle_id: str | None = None, name: str | None = None,
           urls: list[str] | None = None,
           electron_debugging_port: int | None = None,
           new_instance: bool = False) -> dict:
    """Launch an app (backgrounded) and return the raw response, which
    already includes `pid` and a `windows` array — no extra list_windows
    round-trip needed in the common case."""
    payload: dict[str, Any] = {}
    if bundle_id:               payload["bundle_id"] = bundle_id
    if name:                    payload["name"] = name
    if urls:                    payload["urls"] = urls
    if electron_debugging_port: payload["electron_debugging_port"] = electron_debugging_port
    if new_instance:            payload["creates_new_application_instance"] = True
    return call("launch_app", payload)


def activate(app_name: str, settle_s: float = 1.0) -> None:
    """macOS background-launch fix: realise the app's window in the AX tree
    by bringing it forward via AppleScript, then let it settle. One-time
    per cold launch. `bring_to_front` is Windows-only and errors here, so
    AppleScript is the macOS path."""
    subprocess.run(["osascript", "-e", f'tell application "{app_name}" to activate'],
                   capture_output=True, text=True)
    time.sleep(settle_s)


def _window_area(w: dict) -> float:
    b = w.get("bounds") or w.get("frame") or {}
    return float(b.get("width", 0) or 0) * float(b.get("height", 0) or 0)


def first_window_id(launch_response: dict, pid: int, *,
                    title_hint: str | None = None,
                    prefer_largest: bool = True) -> int | None:
    """Pick the best window id for `pid`.

    An app can own many windows (toolbars, popovers, off-screen helpers),
    so the *first* one is usually wrong — for Safari it is a tiny panel,
    not the page. Selection order:
      1. a window whose title contains `title_hint` (case-insensitive), then
      2. the largest-area window (the real content window), else
      3. the first window with a valid id.
    """
    candidates: list[dict] = []
    for w in launch_response.get("windows", []) or []:
        if w.get("pid") == pid and w.get("window_id") is not None:
            candidates.append(w)
    if not candidates:
        wins = call("list_windows", {}).get("windows", []) or []
        candidates = [w for w in wins
                      if w.get("pid") == pid and w.get("window_id") is not None]
    if not candidates:
        return None
    if title_hint:
        hint = title_hint.lower()
        titled = [w for w in candidates
                  if hint in str(w.get("title") or "").lower()]
        if titled:
            return max(titled, key=_window_area)["window_id"]
    if prefer_largest:
        return max(candidates, key=_window_area)["window_id"]
    return candidates[0]["window_id"]


# ── perception ───────────────────────────────────────────────────────────────
def scan(pid: int, window_id: int, *, mode: str = "ax",
         query: str | None = None, guard: bool = True) -> dict:
    """get_window_state. Builds the per-(pid,window) element-index cache and
    returns the parsed response. When `guard` is set, an empty AX tree
    raises PreconditionError naming every candidate cause."""
    payload: dict[str, Any] = {"pid": pid, "window_id": window_id, "capture_mode": mode}
    if query:
        payload["query"] = query
    state = call("get_window_state", payload)
    if guard and mode != "vision" and int(state.get("element_count", 0)) == 0:
        raise PreconditionError(
            f"empty AX tree for pid={pid} window_id={window_id}. Check: "
            "(1) permissions granted, (2) app activated after launch, "
            "(3) Electron app → relaunch with electron_debugging_port + use the "
            "page tool, (4) canvas/game → escalate to vision."
        )
    return state


def screenshot(pid: int, window_id: int, out_file: str) -> str:
    """Capture the window PNG (vision mode = screenshot only, no AX walk)
    and write it to `out_file`. Returns the written path."""
    Path(out_file).parent.mkdir(parents=True, exist_ok=True)
    state = call("get_window_state", {
        "pid": pid, "window_id": window_id,
        "capture_mode": "vision", "screenshot_out_file": out_file,
    })
    return state.get("screenshot_file_path") or out_file


def window_bounds(pid: int, window_id: int) -> dict | None:
    """Return the window's on-screen bounds {x, y, width, height} in LOGICAL
    points, looked up from list_windows. Returns None if not found."""
    for w in call("list_windows", {}).get("windows", []) or []:
        if w.get("window_id") == window_id and w.get("pid") == pid:
            b = w.get("bounds") or w.get("frame")
            if b and b.get("width") and b.get("height"):
                return {"x": float(b.get("x", 0)), "y": float(b.get("y", 0)),
                        "width": float(b["width"]), "height": float(b["height"])}
    return None


def get_screen_size() -> dict:
    """Return {width, height, scale_factor} for the main display — logical
    points + backing scale. Authoritative dpr (no probing)."""
    return call("get_screen_size", {})


def zoom(pid: int, window_id: int, x1: int, y1: int, x2: int, y2: int,
         out_file: str | None = None) -> tuple[bytes, int, int]:
    """Native crop of a window region (x1,y1)-(x2,y2) in SCREENSHOT pixels
    (+20% padding, ≤500px wide). Returns (jpeg_bytes, width, height).

    After this call, pass `from_zoom=True` to click()/type_text so the driver
    auto-translates the zoom-image pixel you pick back to full-window space —
    no manual origin/scale math on our side.
    """
    resp = call("zoom", {"pid": pid, "window_id": window_id,
                         "x1": x1, "y1": y1, "x2": x2, "y2": y2})
    b64 = resp.get("screenshot_png_b64") or ""
    raw = base64.b64decode(b64) if b64 else b""
    if out_file and raw:
        Path(out_file).parent.mkdir(parents=True, exist_ok=True)
        Path(out_file).write_bytes(raw)
    return raw, int(resp.get("width", 0) or 0), int(resp.get("height", 0) or 0)




# ── actions ──────────────────────────────────────────────────────────────────
def press_key(pid: int, key: str, *, modifiers: list[str] | None = None,
              window_id: int | None = None) -> dict:
    """Press one key. Valid keys: return, tab, escape, up/down/left/right,
    space, delete, home, end, pageup, pagedown, f1-f12, and any letter or
    digit. SYMBOLS (* + = …) are NOT key names — use a modified digit
    (e.g. '*' == shift+8) or `hotkey`."""
    payload: dict[str, Any] = {"pid": pid, "key": key}
    if modifiers:  payload["modifiers"] = modifiers
    if window_id is not None:  payload["window_id"] = window_id
    return call("press_key", payload)


def hotkey(pid: int, keys: list[str], *, window_id: int | None = None) -> dict:
    """Press a key combination (>=2 keys, modifiers first). With window_id
    uses the NSMenu path for native menu actions (Cmd+W, Cmd+Z, …)."""
    payload: dict[str, Any] = {"pid": pid, "keys": keys}
    if window_id is not None:  payload["window_id"] = window_id
    return call("hotkey", payload)


def click(pid: int, window_id: int, *, element_index: int | None = None,
          x: int | None = None, y: int | None = None,
          from_zoom: bool = False, debug_image_out: str | None = None) -> dict:
    """Click by element_index (semantic, preferred) or by (x, y) window-local
    screenshot pixels (for canvas/video/WebGL surfaces with no AX node).

    `from_zoom`: x,y are in the LAST zoom image for this pid; the driver
    translates them back to full-window space (pair with cua.zoom).
    `debug_image_out`: driver writes a fresh window screenshot with a red
    crosshair at the actual click point — authoritative verification."""
    payload: dict[str, Any] = {"pid": pid, "window_id": window_id}
    if element_index is not None:
        payload["element_index"] = element_index
    elif x is not None and y is not None:
        payload["x"], payload["y"] = x, y
        if from_zoom:
            payload["from_zoom"] = True
        if debug_image_out:
            payload["debug_image_out"] = debug_image_out
    else:
        raise CuaError("click needs element_index or (x, y)")
    return call("click", payload)


def page(pid: int, action: str, *, selector: str | None = None,
         css_selector: str | None = None, javascript: str | None = None,
         attributes: list[str] | None = None, window_id: int | None = None) -> dict:
    """Drive an Electron/browser DOM via CDP. Actions: execute_javascript,
    get_text, query_dom, click_element."""
    payload: dict[str, Any] = {"pid": pid, "action": action}
    if selector:      payload["selector"] = selector
    if css_selector:  payload["css_selector"] = css_selector
    if javascript:    payload["javascript"] = javascript
    if attributes:    payload["attributes"] = attributes
    if window_id is not None:  payload["window_id"] = window_id
    return call("page", payload, timeout=90.0)


# ── recording ────────────────────────────────────────────────────────────────
def start_recording(output_dir: str, *, record_video: bool = False) -> dict:
    """Record every subsequent action tool into turn-numbered folders under
    `output_dir` — the assignment's submission evidence."""
    os.makedirs(output_dir, exist_ok=True)
    payload: dict[str, Any] = {"output_dir": output_dir}
    if record_video:  payload["record_video"] = True
    return call("start_recording", payload)


def stop_recording() -> dict:
    return call("stop_recording", {})


# ── clipboard (Layer-1 extract) ──────────────────────────────────────────────
def read_clipboard() -> str:
    """Return the macOS clipboard text via `pbpaste`. Used as a $0 Layer-1
    extract to read app state that the AX tree exposes awkwardly — e.g. the
    Calculator result, which Cmd+C copies as plain text."""
    proc = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=10)
    return (proc.stdout or "").strip()


def copy_selection(pid: int) -> None:
    """Press Cmd+C against `pid` so the app puts its current value on the
    clipboard, then leave it for read_clipboard()."""
    hotkey(pid, ["cmd", "c"])
    time.sleep(0.3)
