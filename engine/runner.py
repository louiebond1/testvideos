"""Phase 1 runner: login to SF, execute step 1 of a scenario, record video."""

import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright, Page

from models.dataclasses import TestScenario, StepResult, ScenarioResult
from engine.coach import get_step_guidance, save_successful_pattern


_DEFAULT_SF_URL = "https://hcm-eu10-preview.hr.cloud.sap/login?company=veritasp01T2"


def run_phase1(scenario: TestScenario) -> ScenarioResult:
    """Compatibility wrapper — runs only step 1."""
    return run_scenario(scenario, max_steps=1)


def run_scenario(
    scenario: TestScenario,
    max_steps: int | None = None,
    runs_root: "Path | str | None" = None,
    headless: bool = False,
) -> ScenarioResult:
    """Login to SF, run all (or first *max_steps*) steps, record video."""
    run_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    base = Path(runs_root) if runs_root else Path("runs")
    runs_dir = base / run_id
    runs_dir.mkdir(parents=True, exist_ok=True)

    username = os.environ["SF_USERNAME"]
    password = os.environ["SF_PASSWORD"]
    sf_url = os.getenv("SF_URL", _DEFAULT_SF_URL)

    steps = scenario.steps[:max_steps] if max_steps else scenario.steps
    result = ScenarioResult(scenario_id=scenario.scenario_id, run_id=run_id, passed=False)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, slow_mo=0 if headless else 400)
        context = browser.new_context(
            record_video_dir=str(runs_dir),
            record_video_size={"width": 1280, "height": 720},
            viewport={"width": 1280, "height": 720},
        )
        page = context.new_page()

        try:
            print("  [login] opening SF login page...")
            _login(page, sf_url, username, password)
            print("  [login] logged in successfully")

            feedback_data = _load_feedback(scenario.scenario_id)

            for i, step in enumerate(steps, 1):
                print(f"\n  [step {i}/{len(steps)}] {step.step_id}")
                print(f"           {step.action[:120]}")
                step_feedback = feedback_data.get(step.step_id, "")
                if step_feedback:
                    print(f"  [feedback] {step_feedback[:120]}")
                step_result = _run_step(page, step, str(runs_dir), feedback=step_feedback)
                result.steps.append(step_result)

                status = "PASS" if step_result.passed else "FAIL"
                print(f"  [step {i}] {status} in {step_result.duration_s}s")
                if step_result.error_message:
                    print(f"           error: {step_result.error_message[:200]}")

                # Stop on first failure — later steps depend on prior ones
                if not step_result.passed:
                    print(f"  [halt] step {i} failed — stopping scenario")
                    break

            result.passed = (
                len(result.steps) == len(steps)
                and all(s.passed for s in result.steps)
            )

        except Exception as exc:
            result.passed = False
            print(f"  [runner error] {exc}")
        finally:
            result.ended_at = datetime.utcnow()
            context.close()
            browser.close()

    videos = list(runs_dir.glob("*.webm"))
    if videos:
        result.s3_url = str(videos[0])

    return result


# ── Feedback loader ───────────────────────────────────────────────────────────

def _load_feedback(scenario_id: str) -> dict:
    """Load stored human feedback — client-specific first, then global fallback."""
    import json
    root = Path(__file__).resolve().parent.parent / "storage"
    client_id = os.getenv("CLIENT_ID", "default")

    # Client-specific feedback
    client_file = root / client_id / "step_feedback.json"
    client_data = {}
    if client_file.exists():
        try:
            client_data = json.loads(client_file.read_text()).get(scenario_id, {})
        except Exception:
            pass

    # Global feedback (shared across all clients — legacy flat file)
    global_file = root / "step_feedback.json"
    global_data = {}
    if global_file.exists():
        try:
            global_data = json.loads(global_file.read_text()).get(scenario_id, {})
        except Exception:
            pass

    return {**global_data, **client_data}  # client-specific overrides global


# ── Login ─────────────────────────────────────────────────────────────────────

def _login(page: Page, url: str, username: str, password: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)

    # Wait for the username field — SF uses j_username on the standard login page
    page.wait_for_selector(
        "#j_username, input[name='j_username'], input[autocomplete='username']",
        timeout=20_000,
    )

    page.fill("#j_username, input[name='j_username']", username)
    page.fill("#j_password, input[name='j_password']", password)

    # SF submit button
    page.click(
        "#logOnFormSubmit, input[type='submit'][value*='Log'], button[type='submit']",
        timeout=10_000,
    )
    page.wait_for_load_state("networkidle", timeout=40_000)


# ── Step executor ─────────────────────────────────────────────────────────────

def _run_step(page: Page, step, output_dir: str, feedback: str = "") -> StepResult:
    t0 = time.time()
    last_exc = None

    for attempt in range(3):
        try:
            if attempt > 0:
                page.wait_for_timeout(2000)
                # On retry, take a screenshot and ask the coach what to do
                shot_pre = os.path.join(output_dir, f"{step.step_id}_retry{attempt}.png")
                try:
                    page.screenshot(path=shot_pre, full_page=False)
                except Exception:
                    shot_pre = ""

                if feedback and shot_pre:
                    guidance = get_step_guidance(shot_pre, step.action, step.expected_result, feedback)
                    if guidance:
                        print(f"  [coach] attempt {attempt+1}: {guidance.get('notes','')}")
                        _execute_guidance(page, guidance)
                        # Fall through to _dispatch to verify the action actually worked

            _dispatch(page, step)
            shot = os.path.join(output_dir, f"{step.step_id}.png")
            page.screenshot(path=shot, full_page=False)
            if attempt > 0 and feedback:
                save_successful_pattern(step.action, feedback, {"approach": "coach_guided", "notes": f"succeeded on attempt {attempt+1}"})
            return StepResult(
                step_id=step.step_id,
                passed=True,
                duration_s=round(time.time() - t0, 2),
                screenshot_path=shot,
            )

        except Exception as exc:
            last_exc = exc
            print(f"  [retry {attempt+1}/3] {step.step_id}: {str(exc)[:120]}")

    shot = os.path.join(output_dir, f"{step.step_id}_fail.png")
    try:
        page.screenshot(path=shot, full_page=False)
    except Exception:
        shot = ""
    return StepResult(
        step_id=step.step_id,
        passed=False,
        error_message=str(last_exc),
        duration_s=round(time.time() - t0, 2),
        screenshot_path=shot,
    )


def _execute_guidance(page: Page, guidance: dict) -> None:
    """Execute a structured action returned by the coach."""
    approach = guidance.get("approach", "wait_and_retry")
    wait_ms = guidance.get("wait_before_ms", 500)
    if wait_ms:
        page.wait_for_timeout(wait_ms)

    if approach == "coordinate_click":
        page.mouse.click(guidance["x"], guidance["y"])
        page.wait_for_load_state("networkidle", timeout=20_000)
    elif approach == "text_click":
        exact = guidance.get("exact", False)
        page.get_by_text(guidance["text"], exact=exact).first.click(timeout=8_000)
        page.wait_for_load_state("networkidle", timeout=20_000)
    elif approach == "selector_click":
        page.locator(guidance["selector"]).first.click(timeout=8_000)
        page.wait_for_load_state("networkidle", timeout=20_000)
    elif approach == "wait_and_retry":
        page.wait_for_timeout(guidance.get("wait_ms", 2000))
    # "skip" — do nothing


# ── Action dispatcher ─────────────────────────────────────────────────────────

def _dispatch(page: Page, step) -> None:
    """Map a free-text step action to Playwright calls."""
    action = step.action.lower()
    data = step.test_data or ""

    # ── Navigate to a URL ──────────────────────────────────────────────────────
    if any(k in action for k in ("navigate to the successfactors", "open your browser", "paste the job posting url")):
        url = _first_url(data) or _first_url(step.action)
        if url:
            page.goto(url, wait_until="networkidle", timeout=30_000)
            return

    # ── Module picker navigation (e.g. "Company Info → Position Org Chart") ───
    if "module picker" in action or (
        "navigate" in action and any(sep in step.action for sep in ("→", "->"))
    ):
        dest = _nav_destination(step.action)
        _module_picker_nav(page, dest)
        return

    if "recruiting" in action and ("module picker" in action or "navigate to recruiting" in action):
        _module_picker_nav(page, ["Recruiting"])
        return

    # ── Search the Position Org Chart ─────────────────────────────────────────
    if "search for" in action and ("position" in action or "parent" in action):
        position_num = _extract_position_number(data) or _extract_position_number(step.action)
        if not position_num:
            # Use whatever position is currently visible as the parent
            visible = page.locator("text=/POS\\d+/").first.text_content(timeout=5_000)
            position_num = visible.strip() if visible else None
        if not position_num:
            raise RuntimeError("No parent position number provided or visible")
        _search_position(page, position_num)
        return

    # ── Fill in form fields (check FIRST — step 4's action mentions "selected create") ─
    if "fill in" in action and ("required field" in action or "required fields" in action):
        _fill_position_form(page, data)
        return

    # ── Select a position card and click Action → menu item ───────────────────
    if "select" in action and ("action" in action or "click" in action) and "fill" not in action:
        _select_and_action(page, step.action)
        return

    # ── Click Save / generic named button ─────────────────────────────────────
    if action.strip().startswith("click") or "click the " in action or "click '" in action:
        label = _first_quoted(step.action) or _word_after_click(step.action)
        if label:
            _click_label(page, label)
            return

    # ── Observation / verify steps — no browser action ────────────────────────


# ── Module picker helper ──────────────────────────────────────────────────────

_MENU_ORDER = [
    "Home", "Admin Centre", "Calibration", "Careers", "Company Info",
    "Continuous Feedback", "Continuous Performance", "Development",
    "My Employee File", "Objectives", "Offboarding", "Onboarding",
    "Performance", "Recruiting", "Reporting",
]


def _module_picker_nav(page: Page, path: list[str]) -> None:
    """Open the SF module picker and click items by screen coordinates.

    SF renders the dropdown in a closed shadow root so JS/CSS selectors can't
    reach it. We calculate each item's position from the Home button's bounding
    box instead.
    """
    btn_loc = page.locator("button:has-text('Home')").first
    btn = btn_loc.bounding_box()
    if not btn:
        raise RuntimeError("Module picker Home button not found")

    btn_loc.click(timeout=10_000)
    page.wait_for_timeout(1500)  # wait for dropdown animation to complete

    first_item = path[0]
    if first_item not in _MENU_ORDER:
        raise RuntimeError(f"'{first_item}' not in known menu order — add it to _MENU_ORDER")

    idx = _MENU_ORDER.index(first_item)
    item_h = 30  # px per menu item in the dropdown
    menu_x = btn["x"] + 80
    menu_y = btn["y"] + btn["height"] + 6 + (idx * item_h) + (item_h // 2)

    page.mouse.move(menu_x, menu_y)
    page.wait_for_timeout(300)
    page.mouse.click(menu_x, menu_y)
    page.wait_for_load_state("networkidle", timeout=25_000)

    # Any remaining path items are on the destination page — use normal selectors
    for item in path[1:]:
        page.get_by_text(item, exact=False).first.click(timeout=10_000)
        page.wait_for_load_state("networkidle", timeout=20_000)


def _nav_destination(action_text: str) -> list[str]:
    """Extract the navigation path from action text.

    "From the Module Picker, navigate to Company Info → Position Org Chart"
    → ["Company Info", "Position Org Chart"]
    """
    # Grab everything after 'navigate to' or 'navigate'
    m = re.search(r"navigate to (.+?)(?:\s*$)", action_text, re.IGNORECASE)
    raw = m.group(1) if m else action_text
    parts = [p.strip() for p in re.split(r"[→\->/]", raw)]
    return [p for p in parts if p and "module picker" not in p.lower()]


# ── Position Org Chart helpers ────────────────────────────────────────────────

def _search_position(page: Page, position_num: str) -> None:
    """Verify a position is visible on the Org Chart (acts as our parent)."""
    page.locator("text=/POS\\d+/").first.wait_for(timeout=8_000)
    # Whatever position is visible serves as the parent — step 3 will click it.


def _select_and_action(page: Page, action_text: str) -> None:
    """Click on the position card, click Action, then the named menu item."""
    # Click the position card if visible (POS\d+)
    try:
        page.locator("text=/POS\\d+/").first.click(timeout=5_000)
        page.wait_for_timeout(500)
    except Exception:
        pass  # may already be selected

    # Click 'Action' button
    page.get_by_role("button", name=re.compile("action", re.IGNORECASE)).first.click(
        timeout=10_000
    )
    page.wait_for_timeout(800)

    # Find the menu item to click — look for "Create same level" or "copy position"
    targets = ["Create same level", "Copy Position", "copy position", "Create Position"]
    for t in targets:
        try:
            page.get_by_text(t, exact=False).first.click(timeout=4_000)
            page.wait_for_load_state("networkidle", timeout=20_000)
            return
        except Exception:
            continue
    raise RuntimeError("Could not find 'Create same level / Copy Position' in Action menu")


def _fill_position_form(page: Page, data: str) -> None:
    """Fill the Create-Position form, or accept defaults if it's the Copy dialog."""
    # Path A: SF opened a "Copy Position" dialog — just verify defaults are sensible.
    # Step 5 will click OK to actually create the copy.
    if page.locator("text=Copy Position").first.is_visible(timeout=2_000):
        # The dialog has 'Number of positions to copy' (default 1) — leave it.
        return

    # Path B: full Create Position form with fields. Fill Position Title at minimum.
    values = _parse_kv(data)
    title = values.get("Position Title", "Auto Test Position")

    filled = False
    for selector in [
        "input[aria-label*='Position Title' i]",
        "input[placeholder*='Position Title' i]",
        "label:has-text('Position Title') ~ input",
        "input[name*='title' i]",
    ]:
        try:
            page.locator(selector).first.fill(title, timeout=3_000)
            filled = True
            break
        except Exception:
            continue
    if not filled:
        raise RuntimeError("Could not find Position Title input on the Create Position form")
    page.wait_for_timeout(500)


def _click_label(page: Page, label: str) -> None:
    """Click a button or element matching *label*, with several fallbacks.

    For 'Save' specifically, also try 'OK' since SF modals use OK as primary action.
    """
    label = label.strip().strip("'\"‘’“”")
    candidates = [label]
    if label.lower() == "save":
        candidates.append("OK")  # Copy Position dialog uses OK

    last_err = None
    for cand in candidates:
        attempts = [
            lambda c=cand: page.get_by_role("button", name=c).first.click(timeout=5_000),
            lambda c=cand: page.get_by_text(c, exact=True).first.click(timeout=5_000),
            lambda c=cand: page.locator(f"button:has-text('{c}')").first.click(timeout=5_000),
            lambda c=cand: page.locator(f"text={c}").first.click(timeout=5_000, force=True),
        ]
        for fn in attempts:
            try:
                fn()
                page.wait_for_load_state("networkidle", timeout=20_000)
                return
            except Exception as e:
                last_err = e
    raise RuntimeError(f"Could not click '{label}': {last_err}")


# ── Utilities ─────────────────────────────────────────────────────────────────

def _first_url(text: str) -> str:
    m = re.search(r"https?://\S+", text or "")
    return m.group(0).rstrip(".,)>\"'") if m else ""


def _first_quoted(text: str) -> str:
    # Matches 'text', "text", ‘text’, “text”
    m = re.search(r"[‘’'\"]([^‘’'\"]{2,60})[‘’'\"]", text)
    return m.group(1) if m else ""


def _word_after_click(text: str) -> str:
    """Extract the target name when no quotes are used. e.g. 'Click Save to ...' → 'Save'."""
    m = re.search(r"\bclick\s+(?:the\s+)?([A-Z][A-Za-z ]{1,30})", text)
    return m.group(1).strip() if m else ""


def _extract_position_number(text: str) -> str:
    """Pull a SF position number like 'POS100001' out of free text."""
    m = re.search(r"POS\d{6}", text or "")
    return m.group(0) if m else ""


def _parse_kv(text: str) -> dict[str, str]:
    """Parse 'Key: Value' lines from the test_data column."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip().strip("[]")
    return out
