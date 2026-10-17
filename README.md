# Polyglot & C Lab Quiz Evaluator (v4.1.0 — Production Edition)

Universal LLM-primary and polyglot triage platform for programming and general quizzes at **IISER Bhopal (Data Science & Engineering)**.
It supports **25+ programming and written languages** (`.c`, `.cpp`, `.py`, `.ipynb`, `.java`, `.js`, `.ts`, `.rs`, `.go`, `.sql`, `.r`, `.sh`, `.jl`, `.txt`, `.md`, `.tex`, …) with the exact same ZIP format, upload structure, and single-page web interface:

* **Primary 4-Way AI Triage (`claude-haiku-5-5`, fallback `claude-haiku-4-5-20251001`)**: Evaluates submissions across any programming language, function-only solution, SQL query, or written/rubric question and classifies them into **Needs review (`RED`)**, **Creative approach (`BLUE`)**, **Clear (`GREEN`)**, or **Unverified (`GRAY`)**, while hard deterministic integrity flags remain locked.
* **Deterministic Integrity & Plagiarism Guardrails**: Runs pairwise canonical structural & prose plagiarism (`STRONG` vs `WEAK` with minority-language cohort protection and cross-question collusion corroboration), cross-question stylometric shift detection, Trojan Source / homoglyph scanning (`CVE-2021-42574`), prompt-injection detection, stub detection, and static complexity/spoofing analysis.
* **Polyglot OS Sandbox (`gcc`, `g++`, `python3 -I -B`, `bash`, `node`)**: When runnable test cases and a local runtime exist (and the submission is a full program rather than a function-only snippet), executes tests under `prlimit` with float-tolerance matching and failure-mode diagnosis (`OFF_BY_ONE`, `INTEGER_OVERFLOW`, `SEGFAULT`, `FLOATING_POINT_EXCEPTION`, `CASE_OR_FORMAT_MISMATCH`).
* **College-Scale Production Resilience**: Persists completed evaluations and TA overrides to SQLite (`/app/data/hitl_calibration.db`), automatically resumes running or finished jobs across browser refreshes (`F5`), supports job cancellation, filters out IDE scratch files (`tempCodeRunnerFile.*`, `.vscode`, `.ipynb_checkpoints`), and scales up to 250 students per batch.

## Deploy on Render (One-Shot Blueprint)

1. Push this repository to GitHub (root contains `Dockerfile` and `render.yaml`).
2. In Render Dashboard → **New → Blueprint** → select the repository. Render reads `render.yaml` and provisions the Docker web service (`starter` plan, Singapore region, 1 GB persistent disk at `/app/data`).
3. Set **`ANTHROPIC_API_KEY`** when prompted.
4. Copy the generated **`API_AUTH_TOKEN`** from **Environment** to share with TAs.
