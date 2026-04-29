"""CLI entry point for EX3 TestOps."""

import argparse
import sys
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()


def main() -> None:
    """Parse CLI arguments and dispatch test run."""
    parser = argparse.ArgumentParser(description="EX3 TestOps — SuccessFactors automated testing")
    parser.add_argument("--module", required=True, help="Module to test (e.g. RCM)")
    parser.add_argument("--env", required=True, help="SF environment ID (e.g. veritasp01T2)")
    parser.add_argument("--client", required=True, help="Client name (e.g. veritas)")
    parser.add_argument("--scenario", default=None, help="Run a single scenario ID (e.g. RCM-SC-001)")
    parser.add_argument("--dry-run", action="store_true", help="Parse workbook only — no browser")
    args = parser.parse_args()

    print(f"EX3 TestOps — module={args.module} env={args.env} client={args.client}")

    if args.dry_run:
        print("Dry run mode — parsing workbook only.")
        _dry_run(args)
    else:
        import os
        from engine.parser import parse_workbook
        from engine.runner import run_scenario

        workbook_path = os.path.join("scripts", f"EX3_{args.module}_Workbook_V1_1.xlsx")
        if not os.path.exists(workbook_path):
            print(f"Workbook not found: {workbook_path}")
            sys.exit(1)

        scenarios = parse_workbook(workbook_path)

        if args.scenario:
            scenario = next((s for s in scenarios if s.scenario_id == args.scenario), None)
            if not scenario:
                print(f"Scenario not found: {args.scenario}")
                sys.exit(1)
        else:
            # Default to first non-login scenario
            scenario = next(
                (s for s in scenarios if not s.scenario_id.startswith("LOGIN")),
                scenarios[0],
            )

        print(f"\nScenario : {scenario.scenario_id} — {scenario.name}")
        print(f"Role     : {scenario.role}")
        print(f"Steps    : {len(scenario.steps)} total\n")

        result = run_scenario(scenario)

        print(f"\n{'─' * 50}")
        print(f"Result   : {'PASS ✓' if result.passed else 'FAIL ✗'}")
        print(f"Run ID   : {result.run_id}")
        if result.s3_url:
            print(f"Video    : {result.s3_url}")
        for sr in result.steps:
            mark = "✓" if sr.passed else "✗"
            print(f"  {mark} {sr.step_id}  ({sr.duration_s}s)")
            if sr.screenshot_path:
                print(f"    Screenshot: {sr.screenshot_path}")
            if sr.error_message:
                print(f"    Error     : {sr.error_message}")
        print("Done.")


def _dry_run(args: argparse.Namespace) -> None:
    """Parse the workbook and print scenario/step counts without launching a browser."""
    from engine.parser import parse_workbook
    import os

    workbook_path = os.path.join("scripts", f"EX3_{args.module}_Workbook_V1_1.xlsx")
    if not os.path.exists(workbook_path):
        print(f"Workbook not found: {workbook_path}")
        sys.exit(1)

    scenarios = parse_workbook(workbook_path)
    print(f"\nParsed {len(scenarios)} scenario(s):")
    for s in scenarios:
        print(f"  {s.scenario_id} — {s.name} ({len(s.steps)} steps)")
    print("\nDry run complete.")


if __name__ == "__main__":
    main()
