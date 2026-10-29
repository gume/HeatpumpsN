# Agent Instructions & Constraints

## Environment & Git Setup
- **Workspace Scope:** Do not write anywhere else; use this folder only. Other folders may be read freely.
- **Python Environment:** A virtual environment is set up at `.venv`. Install all missing libraries there. Prefer using `uv`, but `pip` should work as a fallback.
- **Version Control:** Create a local git repo for the work. Commit after successful stages.

## Data & Resource Guidelines
- **Analytics Folder:** Contains pre-analysis. You may use the results from here, but **do not rely on the conclusions**. Any conclusion used must be verified separately.
- **Old Code:** The analytics folder contains an `OLD CODE` folder (the main part of a controller used to produce measurement results). Use it to understand anomalies in the data.
- **Plans Folder:** Contains a planned new controller, to be used only if needed.

## Workflow & Structure
- **Folder Structure:** The final output should be produced in steps defined in the instructions. Each script, report, graph, etc., must go to its own dedicated folder under the `simulation` folder (`step1` is already there and supposed to be finished; `step2` is not finalized yet). Note: Filenames in reports may show different paths because they were moved after creation.
- **Step-by-Step Execution:** Process the instructions step by step. Do not advance to the next step until the previous one is fully finalized.
- **Cleanup:** When a given step is finalized, clean up the working folder and keep only the relevant files.
- **Final Parameters:** Use the main `simulation` folder to collect the final parameter set in a single file.

## Auditing & Reports
- **Rigorous Audits:** When a report is created, perform a very thorough audit (minor mistakes can escalate later). If a problem is found, return to the current step and fix it.
- **Revision Tracking:** Create a document tracking report revisions and their changes, and keep it updated.
- **Self-Contained Reports:** All reports should be understandable without requiring previous versions of reports within the same step (though they may rely on conclusions from previous steps).
