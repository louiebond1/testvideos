"""EX3 TestOps — FastAPI dashboard."""

import json
import os
import sys
import threading
from pathlib import Path
from collections import defaultdict
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))
from engine.parser import parse_workbook  # noqa: E402
from engine.runner import run_scenario  # noqa: E402

CLIENT_ID = os.getenv("CLIENT_ID", "default")

SCRIPTS_DIR = ROOT / "scripts"
RUNS_DIR = ROOT / "runs" / CLIENT_ID
STORAGE_DIR = ROOT / "storage" / CLIENT_ID
STATUS_FILE = STORAGE_DIR / "step_status.json"

RUNS_DIR.mkdir(parents=True, exist_ok=True)
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
(ROOT / "storage" / "global").mkdir(parents=True, exist_ok=True)

# In-memory run state: scenario_id -> {status, run_id, passed?, error?}
_ACTIVE_RUNS: dict[str, dict] = {}

# Pause/resume state: scenario_id -> {event, fix}
_PAUSE_EVENTS: dict[str, threading.Event] = {}
_PAUSE_FIX: dict[str, dict | None] = {}

# Live control — runner thread owns all Playwright calls while paused.
# Server just reads screenshot files and appends to the action queue.
_LIVE_SHOT_PATHS: dict[str, Path] = {}   # scenario_id -> Path of latest PNG
_LIVE_QUEUES: dict[str, list] = {}       # scenario_id -> list of pending actions

# Force-pause: set by UI to pause the runner before the next step
_FORCE_PAUSE: dict[str, bool] = {}


def _humanise_error(raw_error: str) -> str:
    """Ask Claude to translate a raw Playwright/Python error into plain English."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key or not raw_error:
        return raw_error
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content":
                f"Translate this Playwright/Python error into one plain-English sentence that a non-technical person can understand. "
                f"Say what couldn't be found or what timed out, in simple words. No jargon. No code. Max 25 words.\n\nError: {raw_error[:400]}"}],
        )
        return msg.content[0].text.strip()
    except Exception:
        return raw_error


def _pause_callback(scenario_id: str, step_id: str, screenshot_path: str, run_id: str, error_message: str = "", page=None):
    """Called by runner when a step fails — pauses and waits for human fix.

    If a live page is provided, runs a screenshot+action loop in the CALLING
    (runner) thread so Playwright is never touched cross-thread.
    """
    import time as _time

    evt = threading.Event()
    _PAUSE_EVENTS[scenario_id] = evt
    _PAUSE_FIX[scenario_id] = None
    shot_url = f"/runs/{run_id}/{Path(screenshot_path).name}" if screenshot_path else None
    human_error = _humanise_error(error_message)
    _ACTIVE_RUNS[scenario_id].update({
        "status": "paused",
        "paused_step": step_id,
        "screenshot_url": shot_url,
        "error_message": human_error,
        "raw_error": error_message,
    })

    if page is not None:
        # Prepare shared paths / queues
        shot_path = RUNS_DIR / f"{scenario_id}_liveshot.png"
        _LIVE_SHOT_PATHS[scenario_id] = shot_path
        _LIVE_QUEUES[scenario_id] = []

        print(f"  [pause] {scenario_id} paused on {step_id} — live control active")

        # Run screenshot + action loop in THIS (runner) thread while waiting.
        # We poll evt with a short timeout so we can process queued actions.
        while not evt.wait(timeout=0.8):
            # Process any pending actions from the UI
            queue = _LIVE_QUEUES.get(scenario_id, [])
            while queue:
                action = queue.pop(0)
                try:
                    atype = action.get("type")
                    if atype == "click":
                        page.mouse.click(action["x"], action["y"])
                        page.wait_for_timeout(400)
                    elif atype == "type":
                        page.keyboard.type(action["text"], delay=60)
                        page.wait_for_timeout(300)
                    elif atype == "key":
                        page.keyboard.press(action["key"])
                        page.wait_for_timeout(300)
                except Exception as _e:
                    print(f"  [live-action] {_e}")
            # Take a fresh screenshot
            try:
                page.screenshot(path=str(shot_path))
            except Exception:
                pass

        _LIVE_SHOT_PATHS.pop(scenario_id, None)
        _LIVE_QUEUES.pop(scenario_id, None)
    else:
        print(f"  [pause] {scenario_id} paused on {step_id} — waiting up to 10 min for human fix")
        evt.wait(timeout=600)

    fix = _PAUSE_FIX.pop(scenario_id, None)
    _PAUSE_EVENTS.pop(scenario_id, None)
    _ACTIVE_RUNS[scenario_id]["status"] = "running"
    return fix


VALID_STATUSES = {"pass", "fail", "blocked", "not_tested"}
FEEDBACK_FILE = STORAGE_DIR / "step_feedback.json"
APPROVED_FILE = STORAGE_DIR / "approved.json"


def _load_approved() -> dict:
    if APPROVED_FILE.exists():
        try:
            return json.loads(APPROVED_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_approved(data: dict) -> None:
    APPROVED_FILE.write_text(json.dumps(data, indent=2))


def _git_push_approved():
    import subprocess
    try:
        paths = [
            str(APPROVED_FILE.relative_to(ROOT)),
            str(FEEDBACK_FILE.relative_to(ROOT)),
        ]
        for p in paths:
            subprocess.run(["git", "-C", str(ROOT), "add", p], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(ROOT), "commit", "-m", "Update approved playbook [auto]"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(ROOT), "push", "origin", "master"], check=True, capture_output=True)
        print("[approved] pushed to GitHub")
    except Exception as exc:
        print(f"[approved] git push skipped: {exc}")


def _load_feedback() -> dict:
    if FEEDBACK_FILE.exists():
        try:
            return json.loads(FEEDBACK_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_feedback(data: dict) -> None:
    FEEDBACK_FILE.write_text(json.dumps(data, indent=2))


def _load_statuses() -> dict:
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_statuses(data: dict) -> None:
    STATUS_FILE.write_text(json.dumps(data, indent=2))


def _step_status(scenario_id: str, step_id: str) -> str:
    return _load_statuses().get(scenario_id, {}).get(step_id, "not_tested")

app = FastAPI(title="EX3 TestOps")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/runs", StaticFiles(directory=str(RUNS_DIR)), name="runs")


CATEGORY_RULES = [
    ("Pre-Requisites & System Access", lambda s: s.scenario_id.startswith("LOGIN")),
    ("Recruiting (RCM) — End-to-End Lifecycle", lambda s: s.scenario_id.startswith("RCM")),
]


def _load_scenarios():
    workbooks = sorted(SCRIPTS_DIR.glob("EX3_*_Workbook*.xlsx"))
    if not workbooks:
        return []
    return parse_workbook(str(workbooks[0]))


def _scenario_status(scenario_id: str, total_steps: int = 0) -> dict:
    """Return scenario-level status combining manual step marks + latest run."""
    manual = _load_statuses().get(scenario_id, {})
    statuses = list(manual.values())

    if "fail" in statuses:
        status = "fail"
    elif "blocked" in statuses:
        status = "blocked"
    elif statuses and all(s == "pass" for s in statuses) and len(statuses) >= total_steps and total_steps > 0:
        status = "pass"
    else:
        status = "not_tested"

    return {
        "status": status,
        "passed_steps": sum(1 for s in statuses if s == "pass"),
    }


def _role_color(role: str) -> str:
    palette = {
        "Recruiter": "blue",
        "Originator": "emerald",
        "Hiring Manager": "amber",
        "Candidate": "violet",
        "Approver": "rose",
    }
    return palette.get(role, "slate")


def _grouped_scenarios():
    scenarios = _load_scenarios()
    groups = defaultdict(list)
    for s in scenarios:
        for label, predicate in CATEGORY_RULES:
            if predicate(s):
                status = _scenario_status(s.scenario_id, total_steps=len(s.steps))
                groups[label].append({
                    "id": s.scenario_id,
                    "name": s.name,
                    "role": s.role,
                    "role_color": _role_color(s.role),
                    "step_count": len(s.steps),
                    **status,
                })
                break
    return [
        {
            "label": label,
            "scenarios": groups[label],
            "scenario_count": len(groups[label]),
            "step_count": sum(sc["step_count"] for sc in groups[label]),
        }
        for label, _ in CATEGORY_RULES
        if groups[label]
    ]


def _stats():
    scenarios = _load_scenarios()
    statuses = [
        _scenario_status(s.scenario_id, total_steps=len(s.steps))["status"]
        for s in scenarios
    ]
    return {
        "total": len(scenarios),
        "passing": statuses.count("pass"),
        "failing": statuses.count("fail"),
        "blocked": statuses.count("blocked"),
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "groups": _grouped_scenarios(),
            "stats": _stats(),
            "active": "all",
            "client_id": CLIENT_ID,
        },
    )


@app.get("/scenario/{scenario_id}", response_class=HTMLResponse)
def scenario_detail(request: Request, scenario_id: str):
    scenarios = _load_scenarios()
    scenario = next((s for s in scenarios if s.scenario_id == scenario_id), None)
    if not scenario:
        return HTMLResponse("Scenario not found", status_code=404)

    import re as _re
    runs = sorted(RUNS_DIR.iterdir(), reverse=True) if RUNS_DIR.exists() else []
    latest_run = None

    # Collect the most recent screenshot per step across ALL runs (for click-to-train).
    # Prefer _fail shots — they show exactly where it broke.
    step_screenshots: dict[str, str] = {}
    for run in runs:
        if not run.is_dir():
            continue
        for shot in sorted(run.glob(f"{scenario_id}-*.png")):
            base = _re.sub(r'_(fail|retry\d*)$', '', shot.stem)
            url = f"/runs/{run.name}/{shot.name}"
            if base not in step_screenshots or "_fail" in shot.stem:
                step_screenshots[base] = url

    for run in runs:
        if not run.is_dir():
            continue
        shots = sorted(run.glob(f"{scenario_id}-*.png"))
        if not shots:
            continue
        videos = sorted(run.glob("*.webm"))
        # Skip runs with no video AND no non-fail screenshots (incomplete abandoned runs)
        non_fail_shots = [s for s in shots if "_fail" not in s.stem]
        if not videos and not non_fail_shots:
            continue
        trace = run / "trace.zip"
        latest_run = {
            "id": run.name,
            "video_url": f"/runs/{run.name}/{videos[0].name}" if videos else None,
            "trace_url": f"/runs/{run.name}/trace.zip" if trace.exists() else None,
            "screenshots": [
                {
                    "url": f"/runs/{run.name}/{s.name}",
                    "step_id": s.stem,
                    "passed": True,
                }
                for s in shots
            ],
        }
        break

    statuses = _load_statuses().get(scenario_id, {})
    step_statuses = {step.step_id: statuses.get(step.step_id, "not_tested") for step in scenario.steps}

    feedback = _load_feedback().get(scenario_id, {})
    approved = _load_approved().get(scenario_id)

    return templates.TemplateResponse(
        request=request,
        name="scenario.html",
        context={
            "scenario": scenario,
            "role_color": _role_color(scenario.role),
            "run": latest_run,
            "stats": _stats(),
            "step_statuses": step_statuses,
            "step_feedback": feedback,
            "step_screenshots": step_screenshots,
            "approved": approved,
            "client_id": CLIENT_ID,
        },
    )


@app.get("/api/analyse/{scenario_id}")
def analyse_scenario_route(scenario_id: str):
    """Return pre-run analysis: data dependencies and questions to ask."""
    from engine.scenario_analyst import analyse_scenario
    scenarios = _load_scenarios()
    scenario = next((s for s in scenarios if s.scenario_id == scenario_id), None)
    if not scenario:
        raise HTTPException(404, "Scenario not found")
    analysis = analyse_scenario(scenario)

    # Filter out questions for steps that already have complete feedback written,
    # unless that feedback contains a {{placeholder}} (meaning the answer is still needed).
    existing_feedback = _load_feedback().get(scenario_id, {})
    def _needs_question(q: dict) -> bool:
        step_id = q.get("step_id", "")
        fb = existing_feedback.get(step_id, "")
        if not fb:
            return True  # no feedback written — question is relevant
        placeholder = "{{" + q.get("key", "") + "}}"
        return placeholder in fb  # only ask if feedback uses this placeholder
    analysis["questions"] = [q for q in analysis.get("questions", []) if _needs_question(q)]

    # Bulletproof fallback: scan every step's feedback for {{placeholder}} markers
    # and ensure each one has a question. If Claude's analyser missed it (or named
    # the key slightly differently), we still ask. Without this, a step with
    # TYPE: {{target_employee_name}} would type the literal placeholder text.
    import re as _re
    covered_keys = {q.get("key") for q in analysis["questions"]}
    for step_id, fb in existing_feedback.items():
        for match in _re.findall(r"\{\{(\w+)\}\}", fb or ""):
            if match in covered_keys:
                continue
            covered_keys.add(match)
            # Generate a friendly question from the key name
            human = match.replace("_", " ").strip().capitalize()
            analysis["questions"].append({
                "step_id": step_id,
                "key": match,
                "question": f"{human}?",
                "default": "",
            })

    return JSONResponse(analysis)


@app.post("/api/run/{scenario_id}")
async def trigger_run(scenario_id: str, request: Request):
    scenarios = _load_scenarios()
    scenario = next((s for s in scenarios if s.scenario_id == scenario_id), None)
    if not scenario:
        raise HTTPException(404, "Scenario not found")

    if _ACTIVE_RUNS.get(scenario_id, {}).get("status") == "running":
        return JSONResponse({"ok": False, "reason": "already running"}, status_code=409)

    # Accept optional pre-run answers (e.g. proxy_name, candidate_name)
    try:
        body = await request.json()
        pre_answers = body if isinstance(body, dict) else {}
    except Exception:
        pre_answers = {}

    run_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    _ACTIVE_RUNS[scenario_id] = {"status": "running", "run_id": run_id}

    # Live step log written to disk so it survives page reload
    step_log_file = RUNS_DIR / f"{scenario_id}_last_run.json"

    def _write_step_log(steps_so_far: list, run_status: str):
        try:
            step_log_file.write_text(json.dumps({
                "run_id": run_id,
                "status": run_status,
                "steps": steps_so_far,
            }, indent=2))
        except Exception:
            pass

    def _run():
        steps_log = []
        try:
            def _step_done_callback(step_id, passed, error, screenshot_url):
                steps_log.append({
                    "step_id": step_id,
                    "passed": passed,
                    "error": error or "",
                    "screenshot_url": screenshot_url or "",
                })
                _write_step_log(steps_log, "running")

            def _check_pause(sid):
                return _FORCE_PAUSE.pop(sid, False)

            result = run_scenario(scenario, runs_root=RUNS_DIR, headless=True,
                                  pause_callback=lambda **kw: _pause_callback(**kw),
                                  initial_context=pre_answers,
                                  step_done_callback=_step_done_callback,
                                  check_pause_fn=_check_pause)
            _write_step_log(steps_log, "done")
            _ACTIVE_RUNS[scenario_id] = {
                "status": "done",
                "run_id": result.run_id,
                "passed": result.passed,
            }
        except Exception as exc:
            import traceback
            print(f"[RUN ERROR] {scenario_id}: {exc}")
            traceback.print_exc()
            _write_step_log(steps_log, "error")
            _ACTIVE_RUNS[scenario_id] = {
                "status": "error",
                "run_id": run_id,
                "error": str(exc),
            }

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True, "run_id": run_id, "status": "running"})


@app.get("/api/run/{scenario_id}/status")
def run_status(scenario_id: str):
    return JSONResponse(_ACTIVE_RUNS.get(scenario_id, {"status": "idle"}))


@app.get("/api/run/{scenario_id}/steps")
def run_steps(scenario_id: str):
    """Return step-by-step results from the last run (persisted to disk)."""
    f = RUNS_DIR / f"{scenario_id}_last_run.json"
    if f.exists():
        try:
            return JSONResponse(json.loads(f.read_text()))
        except Exception:
            pass
    return JSONResponse({"steps": [], "status": "idle"})


EXPECTED_OVERRIDES_FILE = STORAGE_DIR / "expected_overrides.json"


def _load_overrides() -> dict:
    if EXPECTED_OVERRIDES_FILE.exists():
        try:
            return json.loads(EXPECTED_OVERRIDES_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_overrides(data: dict) -> None:
    EXPECTED_OVERRIDES_FILE.write_text(json.dumps(data, indent=2))


@app.get("/api/expected-override/{scenario_id}/{step_id}")
def get_expected_override(scenario_id: str, step_id: str):
    return JSONResponse({"override": _load_overrides().get(scenario_id, {}).get(step_id, "")})


@app.post("/api/expected-override")
def set_expected_override(scenario_id: str = Form(...), step_id: str = Form(...), override: str = Form(...)):
    data = _load_overrides()
    if override.strip():
        data.setdefault(scenario_id, {})[step_id] = override.strip()
    else:
        data.get(scenario_id, {}).pop(step_id, None)
        if scenario_id in data and not data[scenario_id]:
            data.pop(scenario_id)
    _save_overrides(data)
    return JSONResponse({"ok": True})


@app.post("/api/interpret-fix/{scenario_id}")
async def interpret_fix(scenario_id: str, request: Request):
    """Use Claude Vision to turn a plain-English description into runner commands."""
    body = await request.json()
    description = body.get("description", "").strip()
    screenshot_url = body.get("screenshot_url", "")
    step_id = body.get("step_id", "")

    key = os.getenv("ANTHROPIC_API_KEY")
    if not key or not description:
        return JSONResponse({"commands": "", "note": "no key or description"})

    try:
        import anthropic, base64
        client = anthropic.Anthropic(api_key=key)
        content: list = []

        # Attach screenshot if available
        if screenshot_url:
            parts = screenshot_url.strip("/").split("/")
            if len(parts) >= 3 and parts[0] == "runs":
                shot_path = RUNS_DIR / parts[1] / parts[2]
                if shot_path.exists():
                    img_b64 = base64.standard_b64encode(shot_path.read_bytes()).decode()
                    content.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": img_b64},
                    })

        content.append({
            "type": "text",
            "text": f"""You control a SAP SuccessFactors browser (1280x720) via Playwright.
A test step failed{f' ({step_id})' if step_id else ''}. The human described how to fix it.

Human description: {description}

Generate ONLY the Playwright commands to carry out the fix. Available commands (one per line):
  GOTO: /sf/start#...
  CLICK: button text or visible label
  CLICK_XY: x, y  (pixel coords on 1280x720)
  TYPE: text to type
  PRESS: Key (Enter, ArrowDown, Escape, Tab)
  WAIT: milliseconds
  JS: javascript expression
  FILL: selector | value

Rules:
- If the human mentions a position like "top right", "bottom left", estimate CLICK_XY coords from the screenshot.
- If they name a button/link, use CLICK: that name.
- Output ONLY commands, no explanations, no markdown fences.
- Maximum 5 commands.""",
        })

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": content}],
        )
        commands = msg.content[0].text.strip()
        if commands.startswith("```"):
            commands = "\n".join(commands.split("\n")[1:-1]).strip()
        return JSONResponse({"commands": commands})
    except Exception as exc:
        print(f"[interpret-fix] error: {exc}")
        return JSONResponse({"commands": "", "error": str(exc)})


@app.post("/api/run/{scenario_id}/cancel")
def cancel_run(scenario_id: str):
    """Force-reset a stuck or paused run."""
    if scenario_id in _PAUSE_EVENTS:
        _PAUSE_FIX[scenario_id] = None
        _PAUSE_EVENTS[scenario_id].set()
    _ACTIVE_RUNS.pop(scenario_id, None)
    return JSONResponse({"ok": True})


@app.post("/api/run/{scenario_id}/resume")
async def resume_run(scenario_id: str, request: Request):
    body = await request.json()
    commands = body.get("commands", "").strip()
    comment = body.get("comment", "").strip()
    save_feedback = body.get("save_feedback", True)

    if scenario_id not in _PAUSE_EVENTS:
        raise HTTPException(400, "No paused run for this scenario")

    _PAUSE_FIX[scenario_id] = {"commands": commands, "comment": comment}

    # Only save as step feedback if there was NO existing feedback for this step.
    # If there WAS feedback and it still failed, the resume fix is a one-off correction —
    # don't overwrite the stored sequence or future runs will lose the full command set.
    if save_feedback and commands:
        paused_step = _ACTIVE_RUNS.get(scenario_id, {}).get("paused_step")
        if paused_step:
            data = _load_feedback()
            existing = data.get(scenario_id, {}).get(paused_step, "")
            if not existing:
                data.setdefault(scenario_id, {})[paused_step] = commands
                _save_feedback(data)

    _PAUSE_EVENTS[scenario_id].set()
    return JSONResponse({"ok": True})


# ── Live control endpoints ──────────────────────────────────────────────────────

@app.get("/api/live/{scenario_id}/screenshot")
def live_screenshot(scenario_id: str):
    """Serve the latest screenshot written by the runner's live-control loop."""
    from fastapi.responses import Response
    shot_path = _LIVE_SHOT_PATHS.get(scenario_id)
    if not shot_path or not shot_path.exists():
        raise HTTPException(404, "No live screenshot available — is the run paused?")
    return Response(content=shot_path.read_bytes(), media_type="image/png")


@app.post("/api/live/{scenario_id}/click")
async def live_click(scenario_id: str, request: Request):
    """Queue a click for the runner's live loop to execute."""
    if scenario_id not in _LIVE_QUEUES:
        raise HTTPException(404, "No live session for this scenario")
    body = await request.json()
    _LIVE_QUEUES[scenario_id].append({"type": "click", "x": int(body["x"]), "y": int(body["y"])})
    return JSONResponse({"ok": True})


@app.post("/api/live/{scenario_id}/type")
async def live_type(scenario_id: str, request: Request):
    """Queue a type action for the runner's live loop."""
    if scenario_id not in _LIVE_QUEUES:
        raise HTTPException(404, "No live session for this scenario")
    body = await request.json()
    _LIVE_QUEUES[scenario_id].append({"type": "type", "text": body.get("text", "")})
    return JSONResponse({"ok": True})


@app.post("/api/live/{scenario_id}/key")
async def live_key(scenario_id: str, request: Request):
    """Queue a key press for the runner's live loop."""
    if scenario_id not in _LIVE_QUEUES:
        raise HTTPException(404, "No live session for this scenario")
    body = await request.json()
    _LIVE_QUEUES[scenario_id].append({"type": "key", "key": body.get("key", "")})
    return JSONResponse({"ok": True})


@app.post("/api/live/{scenario_id}/done")
async def live_done(scenario_id: str, request: Request):
    """User finished live control — save recorded commands and resume runner."""
    body = await request.json()
    commands = body.get("commands", "").strip()

    if scenario_id not in _PAUSE_EVENTS:
        raise HTTPException(400, "No paused run for this scenario")

    # Save as step feedback if we recorded anything
    if commands:
        paused_step = _ACTIVE_RUNS.get(scenario_id, {}).get("paused_step")
        if paused_step:
            data = _load_feedback()
            data.setdefault(scenario_id, {})[paused_step] = commands
            _save_feedback(data)
            import threading as _t
            _t.Thread(target=_git_push_feedback, daemon=True).start()

    _PAUSE_FIX[scenario_id] = {"skip": True}
    _PAUSE_EVENTS[scenario_id].set()
    return JSONResponse({"ok": True})


@app.post("/api/live/{scenario_id}/request-control")
async def request_control(scenario_id: str, request: Request):
    """Set force-pause flag so runner pauses before the next step.
    If no run is active, starts one first.
    """
    body = await request.json()
    pre_answers = body.get("answers", {})

    status = _ACTIVE_RUNS.get(scenario_id, {}).get("status", "idle")

    if status == "paused":
        # Already paused — nothing to do, UI will open live control directly
        return JSONResponse({"ok": True, "status": "paused"})

    # Set the flag — runner will pause before the next step
    _FORCE_PAUSE[scenario_id] = True

    if status not in ("running",):
        # Not running — start a fresh run
        scenarios = _load_scenarios()
        scenario = next((s for s in scenarios if s.scenario_id == scenario_id), None)
        if not scenario:
            raise HTTPException(404, "Scenario not found")

        # Cancel any stuck run first
        if scenario_id in _PAUSE_EVENTS:
            _PAUSE_FIX[scenario_id] = None
            _PAUSE_EVENTS[scenario_id].set()

        run_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        _ACTIVE_RUNS[scenario_id] = {"status": "running", "run_id": run_id}
        step_log_file = RUNS_DIR / f"{scenario_id}_last_run.json"

        def _run():
            steps_log = []
            try:
                def _step_done(step_id, passed, error, screenshot_url):
                    steps_log.append({"step_id": step_id, "passed": passed,
                                      "error": error or "", "screenshot_url": screenshot_url or ""})
                    try:
                        step_log_file.write_text(json.dumps({"run_id": run_id, "status": "running", "steps": steps_log}, indent=2))
                    except Exception:
                        pass

                def _check_pause(sid):
                    return _FORCE_PAUSE.pop(sid, False)

                result = run_scenario(scenario, runs_root=RUNS_DIR, headless=True,
                                      pause_callback=lambda **kw: _pause_callback(**kw),
                                      initial_context=pre_answers,
                                      step_done_callback=_step_done,
                                      check_pause_fn=_check_pause)
                try:
                    step_log_file.write_text(json.dumps({"run_id": run_id, "status": "done", "steps": steps_log}, indent=2))
                except Exception:
                    pass
                _ACTIVE_RUNS[scenario_id] = {"status": "done", "run_id": result.run_id, "passed": result.passed}
            except Exception as exc:
                _ACTIVE_RUNS[scenario_id] = {"status": "error", "run_id": run_id, "error": str(exc)}

        import threading as _t
        _t.Thread(target=_run, daemon=True).start()

    return JSONResponse({"ok": True, "status": "starting"})


def _git_push_feedback():
    """Commit and push feedback file to GitHub in the background."""
    import subprocess
    try:
        feedback_path = str(FEEDBACK_FILE.relative_to(ROOT))
        subprocess.run(["git", "-C", str(ROOT), "add", feedback_path], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(ROOT), "commit", "-m", "Update step feedback [auto]"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(ROOT), "push", "origin", "master"], check=True, capture_output=True)
        print("[feedback] pushed to GitHub — Railway redeploying")
    except Exception as exc:
        print(f"[feedback] git push skipped: {exc}")


@app.get("/click/{scenario_id}/{step_id}", response_class=HTMLResponse)
def click_trainer(scenario_id: str, step_id: str):
    """Full-screen click trainer — shows latest screenshot for a step, click = coordinates."""
    import re as _re
    runs = sorted(RUNS_DIR.iterdir(), reverse=True) if RUNS_DIR.exists() else []
    img_url = None
    for run in runs:
        if not run.is_dir():
            continue
        for shot in sorted(run.glob(f"{step_id}*.png"), reverse=True):
            img_url = f"/runs/{run.name}/{shot.name}"
            break
        if img_url:
            break

    if not img_url:
        return HTMLResponse(f"<h2>No screenshot found for {step_id}</h2>", status_code=404)

    feedback_data = _load_feedback()
    current_feedback = feedback_data.get(scenario_id, {}).get(step_id, "")

    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Click Trainer — {step_id}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:#111; color:#fff; font-family:monospace; display:flex; flex-direction:column; height:100vh; }}
  #header {{ background:#1a1a1a; border-bottom:1px solid #333; padding:10px 16px; display:flex; align-items:center; gap:12px; flex-shrink:0; }}
  #header h1 {{ font-size:13px; color:#aaa; }}
  #coords {{ background:#000; color:#0f0; font-size:14px; font-weight:bold; padding:4px 10px; border-radius:4px; min-width:130px; text-align:center; }}
  #copy-btn {{ background:#2563eb; color:#fff; border:none; padding:5px 12px; border-radius:4px; cursor:pointer; font-size:12px; font-family:monospace; }}
  #copy-btn:hover {{ background:#1d4ed8; }}
  #add-btn {{ background:#16a34a; color:#fff; border:none; padding:5px 12px; border-radius:4px; cursor:pointer; font-size:12px; font-family:monospace; }}
  #add-btn:hover {{ background:#15803d; }}
  #img-wrap {{ flex:1; overflow:auto; display:flex; align-items:flex-start; justify-content:center; padding:8px; position:relative; cursor:crosshair; }}
  #shot {{ max-width:100%; display:block; user-select:none; }}
  .dot {{ position:absolute; width:20px; height:20px; background:#ef4444; border:2px solid #fff; border-radius:50%; transform:translate(-50%,-50%); pointer-events:none; box-shadow:0 0 0 2px #ef4444; }}
  .dot-label {{ position:absolute; background:#ef4444; color:#fff; font-size:10px; padding:1px 4px; border-radius:3px; transform:translate(8px,-50%); pointer-events:none; white-space:nowrap; }}
  #cmd-panel {{ background:#1a1a1a; border-top:1px solid #333; padding:10px 16px; flex-shrink:0; display:flex; align-items:center; gap:8px; }}
  #cmd-out {{ flex:1; background:#000; color:#0f0; font-size:12px; padding:6px 10px; border-radius:4px; border:1px solid #333; min-height:32px; word-break:break-all; }}
  #save-btn {{ background:#7c3aed; color:#fff; border:none; padding:6px 14px; border-radius:4px; cursor:pointer; font-size:12px; font-family:monospace; }}
  #save-btn:hover {{ background:#6d28d9; }}
  #status {{ font-size:11px; color:#aaa; }}
</style>
</head>
<body>
<div id="header">
  <h1>{step_id}</h1>
  <div id="coords">click image</div>
  <button id="copy-btn" onclick="copyCoords()">Copy CLICK_XY</button>
  <button id="add-btn" onclick="addToCommands()">Add to commands</button>
  <span id="status"></span>
</div>
<div id="img-wrap">
  <img id="shot" src="{img_url}" draggable="false" />
</div>
<div id="cmd-panel">
  <div id="cmd-out">{current_feedback or "(commands will appear here)"}</div>
  <button id="save-btn" onclick="saveCommands()">Save &amp; close</button>
</div>

<script>
  const SCENARIO_ID = "{scenario_id}";
  const STEP_ID = "{step_id}";
  let lastX = 0, lastY = 0;
  const wrap = document.getElementById('img-wrap');
  const shot = document.getElementById('shot');
  const coords = document.getElementById('coords');
  const cmdOut = document.getElementById('cmd-out');

  wrap.addEventListener('click', (e) => {{
    const rect = shot.getBoundingClientRect();
    const x = Math.round((e.clientX - rect.left) * (1280 / rect.width));
    const y = Math.round((e.clientY - rect.top)  * (720  / rect.height));
    lastX = x; lastY = y;
    coords.textContent = x + ', ' + y;

    // dot
    const dot = document.createElement('div');
    dot.className = 'dot';
    dot.style.left = (e.clientX - wrap.getBoundingClientRect().left) + 'px';
    dot.style.top  = (e.clientY - wrap.getBoundingClientRect().top)  + 'px';
    const lbl = document.createElement('div');
    lbl.className = 'dot-label';
    lbl.style.left = dot.style.left;
    lbl.style.top  = dot.style.top;
    lbl.textContent = x + ',' + y;
    wrap.appendChild(dot);
    wrap.appendChild(lbl);
  }});

  function copyCoords() {{
    navigator.clipboard.writeText('CLICK_XY: ' + lastX + ', ' + lastY);
    document.getElementById('status').textContent = 'copied!';
    setTimeout(() => document.getElementById('status').textContent = '', 1500);
  }}

  function addToCommands() {{
    const cur = cmdOut.textContent.trim();
    const line = 'CLICK_XY: ' + lastX + ', ' + lastY;
    cmdOut.textContent = (cur && cur !== '(commands will appear here)') ? cur + '\\n' + line : line;
  }}

  async function saveCommands() {{
    const text = cmdOut.textContent.trim();
    if (!text || text === '(commands will appear here)') return;
    const fd = new FormData();
    fd.append('scenario_id', SCENARIO_ID);
    fd.append('step_id', STEP_ID);
    fd.append('feedback', text);
    const res = await fetch('/api/step-feedback', {{ method: 'POST', body: fd }});
    if (res.ok) {{
      document.getElementById('status').textContent = 'saved!';
      setTimeout(() => window.close(), 800);
    }}
  }}
</script>
</body>
</html>""")


@app.post("/api/scenario/{scenario_id}/approve")
def approve_scenario(scenario_id: str):
    """Lock the current feedback as the golden playbook — used on every future run."""
    feedback = _load_feedback().get(scenario_id, {})
    approved = _load_approved()
    approved[scenario_id] = {
        "approved_at": datetime.utcnow().isoformat(),
        "step_commands": feedback,
    }
    _save_approved(approved)
    threading.Thread(target=_git_push_approved, daemon=True).start()
    return JSONResponse({"ok": True})


@app.post("/api/scenario/{scenario_id}/unapprove")
def unapprove_scenario(scenario_id: str):
    """Remove the golden playbook so the scenario goes back to normal mode."""
    approved = _load_approved()
    approved.pop(scenario_id, None)
    _save_approved(approved)
    threading.Thread(target=_git_push_approved, daemon=True).start()
    return JSONResponse({"ok": True})


@app.post("/api/step-feedback")
def set_step_feedback(
    scenario_id: str = Form(...),
    step_id: str = Form(...),
    feedback: str = Form(...),
    push: str = Form("true"),   # "false" = save locally only, no git push
):
    data = _load_feedback()
    if feedback.strip():
        data.setdefault(scenario_id, {})[step_id] = feedback.strip()
    else:
        data.get(scenario_id, {}).pop(step_id, None)
    _save_feedback(data)
    if push.lower() != "false":
        threading.Thread(target=_git_push_feedback, daemon=True).start()
    return JSONResponse({"ok": True})


@app.post("/api/step-status")
def set_step_status(scenario_id: str = Form(...), step_id: str = Form(...), status: str = Form(...)):
    if status not in VALID_STATUSES:
        raise HTTPException(400, f"Invalid status; must be one of {VALID_STATUSES}")
    data = _load_statuses()
    data.setdefault(scenario_id, {})[step_id] = status
    if status == "not_tested":
        data[scenario_id].pop(step_id, None)
        if not data[scenario_id]:
            data.pop(scenario_id)
    _save_statuses(data)
    return JSONResponse({"ok": True, "scenario_id": scenario_id, "step_id": step_id, "status": status})
