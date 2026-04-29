"""EX3 TestOps dashboard — upload workbooks, browse runs, watch videos."""

import sys
from pathlib import Path

import streamlit as st

# Make project modules importable when launched via `streamlit run ui/app.py`
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.parser import parse_workbook  # noqa: E402

SCRIPTS_DIR = ROOT / "scripts"
RUNS_DIR = ROOT / "runs"
SCRIPTS_DIR.mkdir(exist_ok=True)
RUNS_DIR.mkdir(exist_ok=True)


st.set_page_config(page_title="EX3 TestOps", layout="wide")
st.title("EX3 TestOps")

tab_runs, tab_upload, tab_workbooks = st.tabs(["Runs", "Upload workbook", "Workbooks"])


# ── Runs tab ──────────────────────────────────────────────────────────────────
with tab_runs:
    runs = sorted([d for d in RUNS_DIR.iterdir() if d.is_dir()], reverse=True)

    if not runs:
        st.info("No runs yet. Run a scenario from the CLI to see it here.")
    else:
        run_labels = [r.name for r in runs]
        selected = st.sidebar.radio("Select a run", run_labels)
        run_path = RUNS_DIR / selected

        st.subheader(f"Run: {selected}")

        videos = sorted(run_path.glob("*.webm"))
        screenshots = sorted(run_path.glob("*.png"))

        col1, col2 = st.columns([2, 1])

        with col1:
            if videos:
                st.markdown("**Video**")
                st.video(str(videos[0]))
            else:
                st.warning("No video found for this run.")

        with col2:
            st.markdown("**Steps**")
            if not screenshots:
                st.write("No screenshots.")
            for shot in screenshots:
                passed = "_fail" not in shot.stem
                mark = "✅" if passed else "❌"
                step_id = shot.stem.replace("_fail", "")
                with st.expander(f"{mark} {step_id}"):
                    st.image(str(shot), use_container_width=True)


# ── Upload tab ────────────────────────────────────────────────────────────────
with tab_upload:
    st.markdown("Upload a test workbook (`.xlsx`). It'll be saved to `scripts/`.")
    uploaded = st.file_uploader("Choose a workbook", type=["xlsx"])
    if uploaded:
        target = SCRIPTS_DIR / uploaded.name
        target.write_bytes(uploaded.getbuffer())
        st.success(f"Saved to {target.relative_to(ROOT)}")

        try:
            scenarios = parse_workbook(str(target))
            st.markdown(f"**Parsed {len(scenarios)} scenarios:**")
            for s in scenarios:
                st.write(f"• `{s.scenario_id}` — {s.name} ({len(s.steps)} steps, role: {s.role})")
        except Exception as exc:
            st.error(f"Failed to parse workbook: {exc}")


# ── Workbooks tab ─────────────────────────────────────────────────────────────
with tab_workbooks:
    workbooks = sorted(SCRIPTS_DIR.glob("*.xlsx"))
    if not workbooks:
        st.info("No workbooks uploaded yet.")
    else:
        wb_names = [w.name for w in workbooks]
        chosen = st.selectbox("Select a workbook", wb_names)
        wb_path = SCRIPTS_DIR / chosen
        try:
            scenarios = parse_workbook(str(wb_path))
            st.markdown(f"**{len(scenarios)} scenarios in `{chosen}`**")
            for s in scenarios:
                with st.expander(f"{s.scenario_id} — {s.name} ({len(s.steps)} steps)"):
                    st.write(f"**Module:** {s.module}  |  **Role:** {s.role}")
                    for step in s.steps:
                        st.markdown(f"**{step.step_id}**")
                        st.write(f"Action: {step.action}")
                        if step.test_data and step.test_data != "—":
                            st.write(f"Data: `{step.test_data}`")
                        st.write(f"Expected: {step.expected_result}")
                        st.divider()
        except Exception as exc:
            st.error(f"Failed to parse workbook: {exc}")
