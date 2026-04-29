"""EX3 TestOps — FastAPI dashboard."""

import json
import os
import sys
import threading
from pathlib import Path
from collections import defaultdict
from datetime import datetime

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parent.parent
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


VALID_STATUSES = {"pass", "fail", "blocked", "not_tested"}
FEEDBACK_FILE = STORAGE_DIR / "step_feedback.json"


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

    runs = sorted(RUNS_DIR.iterdir(), reverse=True) if RUNS_DIR.exists() else []
    latest_run = None
    for run in runs:
        if run.is_dir() and any(run.glob(f"{scenario_id}-*.png")):
            videos = sorted(run.glob("*.webm"))
            shots = sorted(run.glob(f"{scenario_id}-*.png"))
            latest_run = {
                "id": run.name,
                "video_url": f"/runs/{run.name}/{videos[0].name}" if videos else None,
                "screenshots": [
                    {
                        "url": f"/runs/{run.name}/{s.name}",
                        "step_id": s.stem.replace("_fail", ""),
                        "passed": "_fail" not in s.stem,
                    }
                    for s in shots
                ],
            }
            break

    statuses = _load_statuses().get(scenario_id, {})
    step_statuses = {step.step_id: statuses.get(step.step_id, "not_tested") for step in scenario.steps}

    feedback = _load_feedback().get(scenario_id, {})

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
            "client_id": CLIENT_ID,
        },
    )


@app.post("/api/run/{scenario_id}")
def trigger_run(scenario_id: str):
    scenarios = _load_scenarios()
    scenario = next((s for s in scenarios if s.scenario_id == scenario_id), None)
    if not scenario:
        raise HTTPException(404, "Scenario not found")

    if _ACTIVE_RUNS.get(scenario_id, {}).get("status") == "running":
        return JSONResponse({"ok": False, "reason": "already running"}, status_code=409)

    run_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    _ACTIVE_RUNS[scenario_id] = {"status": "running", "run_id": run_id}

    def _run():
        try:
            result = run_scenario(scenario, runs_root=RUNS_DIR, headless=True)
            _ACTIVE_RUNS[scenario_id] = {
                "status": "done",
                "run_id": result.run_id,
                "passed": result.passed,
            }
        except Exception as exc:
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


@app.post("/api/step-feedback")
def set_step_feedback(scenario_id: str = Form(...), step_id: str = Form(...), feedback: str = Form(...)):
    data = _load_feedback()
    if feedback.strip():
        data.setdefault(scenario_id, {})[step_id] = feedback.strip()
    else:
        data.get(scenario_id, {}).pop(step_id, None)
    _save_feedback(data)
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
