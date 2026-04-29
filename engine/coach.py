"""AI coach — uses Claude vision to guide the runner past failures.

Successful coaching patterns are saved to storage/global/sf_notes.md
so every new client deployment starts with accumulated SF knowledge.
"""

import base64
import json
import os
from datetime import datetime
from pathlib import Path

_STORAGE_ROOT = Path(__file__).resolve().parent.parent / "storage"
_GLOBAL_NOTES = _STORAGE_ROOT / "global" / "sf_notes.md"


def _load_global_notes() -> str:
    if _GLOBAL_NOTES.exists():
        return _GLOBAL_NOTES.read_text(encoding="utf-8")
    return ""


def save_successful_pattern(step_action: str, feedback: str, guidance: dict) -> None:
    """Append a successful coaching result to the global SF knowledge base."""
    _GLOBAL_NOTES.parent.mkdir(parents=True, exist_ok=True)
    approach = guidance.get("approach", "unknown")
    notes = guidance.get("notes", "")
    entry = (
        f"\n### {datetime.utcnow().date()} — {step_action[:80]}\n"
        f"- **Feedback given:** {feedback}\n"
        f"- **Solution:** `{approach}` — {notes}\n"
        f"- **Full guidance:** `{json.dumps(guidance)}`\n"
    )
    with open(_GLOBAL_NOTES, "a", encoding="utf-8") as f:
        f.write(entry)


def get_step_guidance(screenshot_path: str, step_action: str, step_expected: str, feedback: str) -> dict | None:
    """Ask Claude to look at the failure screenshot and return structured guidance.

    Returns a dict like:
      {"approach": "coordinate_click", "x": 145, "y": 320, "wait_before_ms": 500, "notes": "..."}
      {"approach": "text_click", "text": "Company Info", "exact": false, "notes": "..."}
      {"approach": "selector_click", "selector": "button.action-btn", "notes": "..."}
      {"approach": "wait_and_retry", "wait_ms": 2000, "notes": "..."}
      {"approach": "skip", "notes": "observation step, no action needed"}
    Returns None if no API key or screenshot unavailable.
    """
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    if not Path(screenshot_path).exists():
        return None

    global_notes = _load_global_notes()
    notes_section = f"\n\nPrevious SF navigation learnings (apply these first):\n{global_notes}" if global_notes else ""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)

        img_data = base64.standard_b64encode(Path(screenshot_path).read_bytes()).decode()

        prompt = f"""You are helping an automated Playwright test runner navigate SAP SuccessFactors.

Step action: {step_action}
Expected result: {step_expected}
Human feedback about what went wrong: {feedback}{notes_section}

Look at the screenshot carefully. Decide the best way for Playwright to complete this step.

Return ONLY valid JSON — no markdown, no explanation outside the JSON:
{{
  "approach": "coordinate_click" | "text_click" | "selector_click" | "wait_and_retry" | "skip",
  "x": <integer pixel x, only if coordinate_click>,
  "y": <integer pixel y, only if coordinate_click>,
  "text": "<visible text to click, only if text_click>",
  "exact": <true|false, only if text_click>,
  "selector": "<CSS selector, only if selector_click>",
  "wait_before_ms": <milliseconds to wait before acting, default 500>,
  "notes": "<one sentence explaining your reasoning>"
}}"""

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": img_data},
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )

        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())

    except Exception as exc:
        return {"approach": "wait_and_retry", "wait_before_ms": 2000, "notes": f"coach error: {exc}"}
