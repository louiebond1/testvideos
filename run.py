"""CLI entry point for EX3 TestOps."""

import argparse
import sys
from dotenv import load_dotenv

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
        print("Full run mode — not yet implemented (Stage 3+).")
        sys.exit(0)


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
