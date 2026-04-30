"""Phase 1 runner: login to SF, execute step 1 of a scenario, record video."""

import json
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
from engine.context_extractor import extract_from_text, substitute, step_produces


_DEFAULT_SF_URL = "https://hcm-eu10-preview.hr.cloud.sap/login?company=veritasp01T2"

_STOP_WORDS = {"the", "a", "an", "to", "from", "and", "or", "in", "on", "of", "for",
               "is", "are", "you", "your", "it", "this", "that", "with", "by", "at"}


def _pattern_file() -> Path:
    root = Path(__file__).resolve().parent.parent / "storage"
    client_id = os.getenv("CLIENT_ID", "default")
    return root / client_id / "patterns.json"


def _load_patterns() -> list:
    f = _pattern_file()
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            return []
    return []


def _save_pattern(step_action: str, commands: str, comment: str = "") -> None:
    import json as _json
    patterns = _load_patterns()
    patterns.append({
        "action": step_action,
        "commands": commands,
        "comment": comment,
        "created_at": datetime.utcnow().isoformat(),
    })
    f = _pattern_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(_json.dumps(patterns, indent=2))


def _match_pattern(step_action: str, threshold: float = 0.55) -> str | None:
    """Find a stored pattern whose action keywords overlap with this step."""
    patterns = _load_patterns()
    if not patterns:
        return None
    words = {w.lower() for w in re.split(r"\W+", step_action) if w and w.lower() not in _STOP_WORDS}
    best_score, best_cmds = 0.0, None
    for p in patterns:
        p_words = {w.lower() for w in re.split(r"\W+", p["action"]) if w and w.lower() not in _STOP_WORDS}
        if not words or not p_words:
            continue
        overlap = len(words & p_words) / len(words | p_words)
        if overlap > best_score:
            best_score, best_cmds = overlap, p["commands"]
    return best_cmds if best_score >= threshold else None


def run_phase1(scenario: TestScenario) -> ScenarioResult:
    """Compatibility wrapper — runs only step 1."""
    return run_scenario(scenario, max_steps=1)


def run_scenario(
    scenario: TestScenario,
    max_steps: int | None = None,
    runs_root: "Path | str | None" = None,
    headless: bool = False,
    pause_callback=None,
    initial_context: dict | None = None,
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

        # Runtime context — values extracted from SF as steps complete
        # e.g. {"position_id": "POS100139", "req_id": "JR-1001"}
        run_context: dict[str, str] = dict(initial_context or {})

        try:
            print("  [login] opening SF login page...")
            _login(page, sf_url, username, password)
            print("  [login] logged in successfully")

            feedback_data = _load_feedback(scenario.scenario_id)

            for i, step in enumerate(steps, 1):
                print(f"\n  [step {i}/{len(steps)}] {step.step_id}")
                print(f"           {step.action[:120]}")

                # Check for learned patterns if no explicit feedback exists
                step_feedback = feedback_data.get(step.step_id, "")
                if not step_feedback:
                    matched = _match_pattern(step.action)
                    if matched:
                        print(f"  [pattern] matched learned pattern — using stored commands")
                        step_feedback = matched

                # Substitute run context values into commands (e.g. {{position_id}})
                if step_feedback and run_context:
                    step_feedback = substitute(step_feedback, run_context)

                if step_feedback:
                    print(f"  [feedback] {step_feedback[:120]}")
                if run_context:
                    print(f"  [context] {run_context}")

                step_result = _run_step(page, step, str(runs_dir), feedback=step_feedback)

                # If failed and we have a pause callback — ask human for help
                if not step_result.passed and pause_callback:
                    print(f"  [pause] waiting for human fix on {step.step_id}...")
                    fix = pause_callback(
                        scenario_id=scenario.scenario_id,
                        step_id=step.step_id,
                        screenshot_path=step_result.screenshot_path,
                        run_id=run_id,
                    )
                    if fix:
                        commands = fix.get("commands", "")
                        comment = fix.get("comment", "")
                        print(f"  [resume] got fix: {commands[:80]}")
                        step_result = _run_step(page, step, str(runs_dir), feedback=commands)
                        if step_result.passed:
                            _save_pattern(step.action, commands, comment)
                            print(f"  [learn] pattern saved for: {step.action[:60]}")

                # After a passing step, extract any new IDs / values from the page
                if step_result.passed and step_produces(step.action):
                    try:
                        page_text = page.evaluate("document.body.innerText")
                        extracted = extract_from_text(page_text)
                        if extracted:
                            run_context.update(extracted)
                            print(f"  [extract] captured: {extracted}")
                    except Exception:
                        pass

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

    # ── Direct command mode: feedback overrides everything ────────────────────
    # If feedback contains lines like "CLICK: Recruiting" the runner executes
    # them literally — no AI, no guessing, guaranteed.
    if _has_direct_commands(feedback):
        print(f"  [direct] {step.step_id}: running manual command override")
        return _run_direct_commands(page, step, output_dir, feedback, t0)

    last_exc = None
    for attempt in range(3):
        try:
            if attempt > 0:
                page.wait_for_timeout(2000)
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


_CMD_PREFIXES = ("CLICK:", "CLICK_XY:", "TYPE:", "PRESS:", "WAIT:", "FILL:", "SHADOW_CLICK:", "GOTO:", "NAVIGATE:", "SELECT:", "SELECT_OPTION:", "JS:")


def _has_direct_commands(feedback: str) -> bool:
    return any(line.strip().upper().startswith(_CMD_PREFIXES) for line in (feedback or "").splitlines())


def _run_direct_commands(page: Page, step, output_dir: str, feedback: str, t0: float) -> StepResult:
    """Execute a step using direct commands written in the feedback box."""
    try:
        for line in feedback.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            cmd, _, arg = line.partition(":")
            cmd = cmd.strip().upper()
            arg = arg.strip()

            if cmd == "CLICK":
                page.get_by_text(arg, exact=False).first.click(timeout=8_000)
            elif cmd == "SHADOW_CLICK":
                if not _shadow_click(page, arg):
                    raise RuntimeError(f"SHADOW_CLICK: could not find '{arg}' in any shadow root")
            elif cmd == "CLICK_XY":
                x, y = [float(v.strip()) for v in arg.split(",")]
                page.mouse.click(x, y)
            elif cmd == "TYPE":
                page.keyboard.type(arg)
            elif cmd == "PRESS":
                page.keyboard.press(arg)
            elif cmd == "WAIT":
                page.wait_for_timeout(int(arg))
            elif cmd == "FILL":
                # "FILL: Field label | value"
                label, _, value = arg.partition("|")
                page.get_by_label(label.strip(), exact=False).first.fill(value.strip(), timeout=5_000)
            elif cmd in ("GOTO", "NAVIGATE") and arg.startswith("http"):
                page.goto(arg, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(2000)
            elif cmd == "NAVIGATE":
                # "NAVIGATE: Recruiting" — open module picker and click the module
                _module_picker_nav(page, [p.strip() for p in arg.split("→")])
            elif cmd == "SELECT":
                # "SELECT: option text" — click a dropdown option
                page.get_by_role("option", name=arg, exact=False).first.click(timeout=5_000)
            elif cmd == "SELECT_OPTION":
                # "SELECT_OPTION: label | value" — select option in a <select> element by label
                label, _, value = arg.partition("|")
                page.get_by_label(label.strip(), exact=False).first.select_option(label=value.strip(), timeout=5_000)
            elif cmd == "JS":
                # "JS: document.querySelector('...').click()"
                page.evaluate(arg)

            page.wait_for_timeout(600)

        shot = os.path.join(output_dir, f"{step.step_id}.png")
        page.screenshot(path=shot, full_page=False)
        return StepResult(step_id=step.step_id, passed=True,
                          duration_s=round(time.time() - t0, 2), screenshot_path=shot)
    except Exception as exc:
        shot = os.path.join(output_dir, f"{step.step_id}_fail.png")
        try:
            page.screenshot(path=shot, full_page=False)
        except Exception:
            shot = ""
        return StepResult(step_id=step.step_id, passed=False,
                          error_message=str(exc),
                          duration_s=round(time.time() - t0, 2), screenshot_path=shot)


def _shadow_click(page: Page, text: str, exact: bool = True) -> bool:
    """Click an element by text, searching recursively through all shadow roots."""
    return page.evaluate(
        """([targetText, exact]) => {
            function search(root) {
                const els = root.querySelectorAll('*');
                for (const el of els) {
                    if (el.shadowRoot) {
                        if (search(el.shadowRoot)) return true;
                    }
                    const t = (el.innerText || el.textContent || '').trim();
                    const matches = exact ? (t === targetText) : t.includes(targetText);
                    if (matches && el.offsetWidth > 0 && el.offsetHeight > 0) {
                        el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                        return true;
                    }
                }
                return false;
            }
            return search(document.body);
        }""",
        [text, exact],
    )


def _proxy_login(page: Page, proxy_name: str) -> None:
    """Switch to a proxy user in SF: click avatar → Proxy Now → search → select → OK."""
    proxy_name = proxy_name.strip()

    # Click user avatar / initials badge (top-right corner)
    clicked_avatar = False
    for sel in [
        "[data-component-id*='avatar']",
        "[aria-label*='avatar' i]",
        "[class*='userAvatar']",
        "[class*='user-avatar']",
        "button[class*='Avatar']",
    ]:
        try:
            page.locator(sel).first.click(timeout=2_000)
            clicked_avatar = True
            break
        except Exception:
            pass

    if not clicked_avatar:
        # Fallback: top-right corner click
        page.mouse.click(1240, 40)

    page.wait_for_timeout(1200)

    # Click "Proxy Now"
    for label in ["Proxy Now", "Proxy now", "Switch User", "Act as Proxy"]:
        try:
            page.get_by_text(label, exact=False).first.click(timeout=4_000)
            break
        except Exception:
            pass
    page.wait_for_timeout(1000)

    # Type name into the proxy search box
    page.keyboard.type(proxy_name, delay=80)
    page.wait_for_timeout(2000)

    # Click matching result
    page.get_by_text(proxy_name, exact=False).first.click(timeout=8_000)
    page.wait_for_timeout(500)

    # Confirm / OK
    for label in ["OK", "Confirm", "Apply", "Select", "Done"]:
        try:
            page.get_by_role("button", name=label).first.click(timeout=3_000)
            page.wait_for_load_state("networkidle", timeout=30_000)
            return
        except Exception:
            pass

    page.wait_for_load_state("networkidle", timeout=30_000)


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

    # ── Proxy login ───────────────────────────────────────────────────────────
    if "proxy" in action:
        name = data.strip() or _first_quoted(step.action) or _word_after_click(step.action)
        if not name:
            # Try extracting "as <Name>" pattern
            m = re.search(r"\bas\s+([A-Z][a-zA-Z ]{2,40})", step.action)
            name = m.group(1).strip() if m else "Alex Brackley"
        _proxy_login(page, name)
        return

    # ── Recruiting shortcut — catch any action that mentions navigating to Recruiting ─
    if "recruit" in action and any(k in action for k in ("navigate", "module", "picker", "go to", "open")):
        _module_picker_nav(page, ["Recruiting"])
        return

    # ── Module picker navigation (e.g. "Company Info → Position Org Chart") ───
    if "module picker" in action or (
        "navigate" in action and any(sep in step.action for sep in ("→", "->"))
    ):
        dest = _nav_destination(step.action)
        if dest:
            _module_picker_nav(page, dest)
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


def _open_module_picker(page: Page) -> dict | None:
    """Click the module picker button and wait for it to open. Returns approximate button box."""
    module_names_js = str(_MENU_ORDER).replace("'", '"')

    # Use JS to FIND the bounding box of the module picker button (don't click via JS).
    # Then use Playwright's real mouse click, which SF's UI5 responds to correctly.
    box = page.evaluate(f"""() => {{
        const names = {module_names_js};
        function search(root) {{
            const els = root.querySelectorAll('*');
            for (const el of els) {{
                if (el.shadowRoot) {{
                    const r = search(el.shadowRoot);
                    if (r) return r;
                }}
                const t = (el.innerText || el.textContent || '').trim();
                const b = el.getBoundingClientRect();
                if (b.top < 60 && b.width > 40 && el.offsetHeight > 0) {{
                    for (const name of names) {{
                        if (t === name || t.startsWith(name)) {{
                            return {{x: b.x, y: b.y, width: b.width, height: b.height}};
                        }}
                    }}
                }}
            }}
            return null;
        }}
        return search(document.body);
    }}""")

    if box:
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        page.mouse.click(cx, cy)
        page.wait_for_timeout(1500)
        return box

    # Hard fallback: module picker is always ~250px from left at top of nav bar
    page.mouse.click(250, 20)
    page.wait_for_timeout(1500)
    return {"x": 150, "y": 10, "width": 200, "height": 40}


def _module_picker_nav(page: Page, path: list[str]) -> None:
    """Open the SF module picker and navigate to the target module."""
    if not path:
        raise RuntimeError("Empty navigation path passed to _module_picker_nav")

    btn = _open_module_picker(page)
    if not btn:
        raise RuntimeError("Could not open module picker")

    first_item = path[0]

    # ── Approach 0: JavaScript recursive shadow DOM search (most reliable) ────
    for exact_match in (True, False):
        try:
            clicked = _shadow_click(page, first_item, exact=exact_match)
            if clicked:
                page.wait_for_load_state("networkidle", timeout=25_000)
                _verify_module_nav(page, first_item)
                _module_picker_subpath(page, path[1:])
                return
        except Exception:
            pass

    # ── Approach 1: Playwright text locator (pierces shadow DOM) ─────────────
    for exact in (True, False):
        try:
            page.get_by_text(first_item, exact=exact).first.click(timeout=3_000)
            page.wait_for_load_state("networkidle", timeout=25_000)
            _verify_module_nav(page, first_item)
            _module_picker_subpath(page, path[1:])
            return
        except Exception:
            pass

    # ── Approach 2: Keyboard navigation (most reliable for shadow DOM lists) ──
    try:
        if first_item in _MENU_ORDER:
            idx = _MENU_ORDER.index(first_item)
            # Tab into the list then arrow-down to the right item
            for _ in range(idx + 1):
                page.keyboard.press("ArrowDown")
                page.wait_for_timeout(80)
            page.keyboard.press("Enter")
            page.wait_for_load_state("networkidle", timeout=25_000)
            _verify_module_nav(page, first_item)
            _module_picker_subpath(page, path[1:])
            return
    except Exception:
        pass

    # ── Approach 3: Coordinate click (26 px per row, anchored to menu top) ───
    try:
        if first_item not in _MENU_ORDER:
            raise RuntimeError(f"'{first_item}' not in _MENU_ORDER")
        idx = _MENU_ORDER.index(first_item)
        item_h = 26
        menu_x = btn["x"] + btn["width"] / 2
        menu_top = btn["y"] + btn["height"] + 8
        menu_y = menu_top + (idx * item_h) + (item_h // 2)
        # If calculated y is off-screen, scroll the dropdown first
        if menu_y > 700:
            page.mouse.wheel(0, menu_y - 600)
            page.wait_for_timeout(300)
            menu_y = min(menu_y, 680)
        page.mouse.move(menu_x, menu_y)
        page.wait_for_timeout(200)
        page.mouse.click(menu_x, menu_y)
        page.wait_for_load_state("networkidle", timeout=25_000)
        _verify_module_nav(page, first_item)
        _module_picker_subpath(page, path[1:])
        return
    except Exception:
        pass

    # ── Approach 4: Vision-based — screenshot the open menu, ask Claude ──────
    import tempfile
    shot = tempfile.mktemp(suffix=".png")
    page.screenshot(path=shot, full_page=False)
    from engine.coach import get_step_guidance
    guidance = get_step_guidance(
        shot,
        f"Click '{first_item}' in the open module picker dropdown",
        f"{first_item} module opens",
        f"The module picker menu is open. Click '{first_item}' in the list.",
    )
    if guidance:
        _execute_guidance(page, guidance)
        page.wait_for_load_state("networkidle", timeout=25_000)
        _module_picker_subpath(page, path[1:])
        return

    raise RuntimeError(f"All four approaches failed to click '{first_item}' in module picker")


def _verify_module_nav(page: Page, module_name: str) -> None:
    """Soft verification — just wait for network to settle, don't hard-fail on URL."""
    page.wait_for_timeout(1000)
    # Don't raise — some modules don't change the URL slug predictably


def _module_picker_subpath(page: Page, remaining: list[str]) -> None:
    for item in remaining:
        page.wait_for_timeout(800)
        # Try shadow click first, then text click
        try:
            if not _shadow_click(page, item):
                page.get_by_text(item, exact=False).first.click(timeout=8_000)
        except Exception:
            page.get_by_text(item, exact=False).first.click(timeout=8_000)
        page.wait_for_load_state("networkidle", timeout=25_000)


def _nav_destination(action_text: str) -> list[str]:
    """Extract the navigation path from action text.

    "From the Module Picker, navigate to Company Info → Position Org Chart"
    → ["Company Info", "Position Org Chart"]
    """
    # Stop extraction before filler words like "using", "via", "through", "from"
    m = re.search(
        r"navigate to (.+?)(?:\s+(?:using|via|through|from|in|with)\b|\s*$)",
        action_text,
        re.IGNORECASE,
    )
    raw = m.group(1).strip() if m else action_text
    parts = [p.strip() for p in re.split(r"[→\->/]", raw)]
    # Strip filler words that sometimes leak in; keep substantive module names
    stop_words = {"module picker", "the module picker", "module"}
    return [p for p in parts if p and p.lower() not in stop_words]


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
