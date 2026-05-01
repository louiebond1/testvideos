"""
Pre-run scenario analysis — reads the full test script, identifies data
dependencies between steps, and flags anything the runner needs to know
before it starts executing.
"""

import json
import os
from models.dataclasses import TestScenario


def analyse_scenario(scenario: TestScenario) -> dict:
    """
    Ask Claude to read the full scenario and return smart, scenario-specific
    questions — only the things a human genuinely needs to decide before the
    run starts (e.g. which position to copy, who to proxy as).
    Falls back to a simple local analysis if no API key.
    """
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return _local_analysis(scenario)

    steps_text = "\n".join(
        f"  {s.step_id}: {s.action}"
        + (f" | data: {s.test_data}" if s.test_data and s.test_data != "—" else "")
        + (f" | expected: {s.expected_result}" if s.expected_result else "")
        for s in scenario.steps
    )

    prompt = f"""You are analysing a SAP SuccessFactors test scenario that will run automatically via Playwright.

Scenario: {scenario.scenario_id} — {scenario.name}
Role: {scenario.role}
Module: {scenario.module}

Steps:
{steps_text}

Your job is to identify the MINIMUM set of questions a human must answer BEFORE the run starts.

Rules:
- Only ask about things where the answer genuinely changes what gets clicked or typed in SF.
- Think about the INTENT of each step. For a "copy position" scenario, the key question is WHICH position to copy — not what department/location to fill in, because those are inherited from the copy.
- For a proxy scenario, ask WHO to proxy as.
- For a job req scenario, ask things like which position it's for, what job title, which hiring manager.
- Do NOT ask about things that are fixed process steps (clicking Save, navigating to a module, confirming dialogs).
- Do NOT ask about technical fields that won't vary (checkboxes that are always ticked, standard navigation).
- If the test data column already has a specific value, do NOT ask about it — use that value as the default.
- Keep questions short and plain English, like you're asking a colleague.
- Maximum 3 questions. If nothing is genuinely needed, return an empty list.

Also identify data dependencies — steps that CREATE a value (position ID, req ID) that a later step needs.

Return ONLY valid JSON, no markdown:
{{
  "dependencies": [
    {{"producer_step": "RCM-RC-101-05", "key": "position_id", "description": "newly created position ID"}}
  ],
  "questions": [
    {{"step_id": "RCM-RC-101-03", "key": "source_position", "question": "Which position should I copy? (e.g. POS100121)", "default": "POS100121"}}
  ]
}}"""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:-1])
        return json.loads(raw)
    except Exception as exc:
        print(f"  [analyst] Claude analysis failed: {exc} — using local fallback")
        return _local_analysis(scenario)


def _local_analysis(scenario: TestScenario) -> dict:
    """Simple keyword-based fallback when Claude is unavailable."""
    from engine.context_extractor import step_produces, step_needs
    deps = []
    questions = []
    producers: dict[str, str] = {}

    for step in scenario.steps:
        for key in step_produces(step.action):
            producers[key] = step.step_id
        for key in step_needs(step.action, step.test_data):
            if key in producers:
                deps.append({
                    "producer_step": producers[key],
                    "consumer_step": step.step_id,
                    "key": key,
                    "description": f"{key} from {producers[key]}",
                })
        # Only flag genuinely open-ended inputs
        td = step.test_data or ""
        if "input name" in td.lower() or "enter name" in td.lower():
            questions.append({
                "step_id": step.step_id,
                "key": step.step_id.lower().replace("-", "_") + "_name",
                "question": f"What name should be used for: {td}?",
                "default": "",
            })

    return {"dependencies": deps, "questions": questions}


def build_context_hints(analysis: dict, answers: dict) -> dict:
    return {q["key"]: answers.get(q["key"], q.get("default", ""))
            for q in analysis.get("questions", [])
            if answers.get(q["key"]) or q.get("default")}
