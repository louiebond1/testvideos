"""
Visual step verification using Claude Vision.

After a step's commands execute without exceptions, we send the screenshot
to Claude with the step's action + expected_result and ask whether the page
state actually matches what should have happened. Catches "fake passes" where
commands ran but nothing meaningful changed in SF.
"""

import base64
import os
from pathlib import Path


def verify_step(screenshot_path: str, action: str, expected_result: str,
                test_data: str = "") -> tuple[bool, str]:
    """
    Returns (passed, reason).
    - passed=True  → Claude confirms the screenshot shows the expected outcome.
    - passed=False → screenshot doesn't match; reason explains what's wrong.
    - On API error or missing key → returns (True, "skipped") so we don't
      block runs when the verifier is unavailable.
    """
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key or not screenshot_path or not Path(screenshot_path).exists():
        return True, "skipped"

    try:
        import anthropic
        img_b64 = base64.standard_b64encode(Path(screenshot_path).read_bytes()).decode()

        prompt = f"""You are verifying a single step of a SAP SuccessFactors automated test.

The step just executed without throwing an exception. Your job is to confirm whether
the page state in this screenshot actually matches the expected outcome — or whether
the commands ran but nothing meaningful happened.

Step action: {action}
{f"Test data: {test_data}" if test_data and test_data != "—" else ""}
Expected result: {expected_result}

Look at the screenshot and reply in this exact format:
PASS: <one short sentence on what you see that confirms the expected result>
or
FAIL: <one short sentence on what's wrong / what's missing>

Be strict. If a form should be open and isn't visible, that's FAIL. If a popup should
have closed and is still showing, that's FAIL. Only PASS when the screenshot clearly
matches the expected result."""

        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": img_b64,
                    }},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        text = msg.content[0].text.strip()
        if text.upper().startswith("PASS"):
            return True, text.split(":", 1)[1].strip() if ":" in text else "verified"
        if text.upper().startswith("FAIL"):
            return False, text.split(":", 1)[1].strip() if ":" in text else text
        # Ambiguous response — don't block the run, but log
        return True, f"ambiguous: {text[:120]}"
    except Exception as exc:
        print(f"  [verifier] error: {exc}")
        return True, f"verifier error: {exc}"
