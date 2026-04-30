"""
Pre-run scenario analysis — reads the full test script, identifies data
dependencies between steps, and flags anything the runner needs to know
before it starts executing.
"""

import json
import os
from pathlib import Path

from models.dataclasses import TestScenario


def analyse_scenario(scenario: TestScenario) -> dict:
    """
    Ask Claude to read the full scenario and return:
      - per-step: what it produces, what it needs, any questions
      - overall: list of questions to ask the human before running
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

    prompt = f"""You are analysing a SAP SuccessFactors test scenario before it runs automatically.

Scenario: {scenario.scenario_id} — {scenario.name}
Role: {scenario.role}
Module: {scenario.module}

Steps:
{steps_text}

Your job:
1. Identify which steps CREATE a value that later steps need (e.g. a position ID, job req ID, offer ID).
2. Identify which steps CONSUME a value from a prior step.
3. List any questions that must be answered BEFORE the run starts (e.g. "Which employee should be used as the candidate?").
   Only ask if the step says "Input name of..." or similar open-ended data requirement with no value given.

Return ONLY valid JSON, no markdown:
{{
  "dependencies": [
    {{"producer_step": "RCM-RC-101-05", "key": "position_id", "description": "newly created position ID"}},
    {{"consumer_step": "RCM-RC-102-02", "key": "position_id", "description": "select the position just created"}}
  ],
  "questions": [
    {{"step_id": "LOGIN-102-03", "key": "proxy_name", "question": "Who should be proxied as?", "default": ""}}
  ]
}}"""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
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
        # Flag open-ended data fields
        if step.test_data and "input" in step.test_data.lower():
            questions.append({
                "step_id": step.step_id,
                "key": step.step_id.lower().replace("-", "_"),
                "question": f"What value should be used for: {step.test_data}?",
                "default": "",
            })

    return {"dependencies": deps, "questions": questions}


def build_context_hints(analysis: dict, answers: dict) -> dict:
    """
    Turn pre-run answers into initial run context.
    answers = {"proxy_name": "Alex Brackley", ...}
    """
    return {q["key"]: answers.get(q["key"], q.get("default", ""))
            for q in analysis.get("questions", [])
            if answers.get(q["key"]) or q.get("default")}
