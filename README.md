# 🎓 IISER Bhopal — General Quiz & Lab Exam Checker (v5.1 Production Edition)
### Universal Polyglot Programming Evaluator & 100% Laptop-Hosted Physical Paper Scanner Hub

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![OpenCV](https://img.shields.io/badge/OpenCV-4.10%2B-5C3EE8.svg)](https://opencv.org/)
[![Anthropic Claude](https://img.shields.io/badge/Claude-Haiku%205.5%20Cached-orange.svg)](https://www.anthropic.com/)
[![Ollama Offline](https://img.shields.io/badge/Ollama-100%25%20Offline%20VLM-black.svg)](https://ollama.com/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

Developed for the **Department of Data Science & Engineering, IISER Bhopal**, this platform provides a unified, production-grade examination evaluation ecosystem featuring **two specialized grading engines**:

```
+-------------------------------------------------------------------------------------------------------+
|                                    IISER BHOPAL QUIZ EVALUATOR                                        |
+--------------------------------------------------+----------------------------------------------------+
|  ENGINE 1: POLYGLOT CODING LAB CHECKER           |  ENGINE 2: PHYSICAL PAPER EXAM CHECKER & HUB       |
|  - Web UI: http://localhost:8000/                |  - Web UI: http://localhost:8000/scanner           |
|  - 25+ Programming & Markup Languages           |  - Mobile Smartphone Camera Scanning over Wi-Fi    |
|  - OS-Level Sandboxing & Timeout Guards          |  - ArUco Fiducial Alignment & Perspective Warping  |
|  - Automatic LeetCode Driver Synthesis           |  - Multi-Page (1..25) Stitched Continuity Tracking |
|  - Winnowing & AST Plagiarism Forensics          |  - Prompt-Cached Claude Haiku 5.5 (90% Savings)    |
|  - 4-Tier Automated Triage (GREEN, BLUE, RED)    |  - 100% Air-Gapped Ollama / Qwen2.5-VL Fallback    |
|  - Gradebook CSV & JSON Export                   |  - Multi-Sheet Excel (.xlsx) & Feedback PDF Zip    |
+--------------------------------------------------+----------------------------------------------------+
```

---

## Table of Contents

1. [Architectural Highlights](#architectural-highlights)
2. [Quick-Start Guide](#quick-start-guide)
   - [Windows (1-Click)](#windows-1-click)
   - [macOS & Linux (1-Click)](#macos--linux-1-click)
   - [Docker Deployment](#docker-deployment)
3. [Engine 1: Polyglot Coding Lab Checker Guide](#engine-1-polyglot-coding-lab-checker-guide)
   - [1.1 Supported Languages & File Ingestion](#11-supported-languages--file-ingestion)
   - [1.2 Question Paper JSON Specification](#12-question-paper-json-specification)
   - [1.3 Concrete Question Paper Examples](#13-concrete-question-paper-examples)
   - [1.4 Sandbox Execution & Driver Synthesis](#14-sandbox-execution--driver-synthesis)
   - [1.5 Deterministic Plagiarism & Collusion Forensics](#15-deterministic-plagiarism--collusion-forensics)
   - [1.6 The 4 Triage Queues (GREEN, BLUE, RED, GRAY)](#16-the-4-triage-queues-green-blue-red-gray)
4. [Engine 2: Physical Paper Exam Checker & Grading Hub Guide](#engine-2-physical-paper-exam-checker--grading-hub-guide)
   - [2.1 Dual Exam Paper Formats](#21-dual-exam-paper-formats)
   - [2.2 Printable Exam Booklet PDF Generation](#22-printable-exam-booklet-pdf-generation)
   - [2.3 Exam Hall Ingestion & Mobile Camera Scanner](#23-exam-hall-ingestion--mobile-camera-scanner)
   - [2.4 Computer Vision Rectification & Handwriting Sharpening](#24-computer-vision-rectification--handwriting-sharpening)
   - [2.5 Dual AI Vision Engines (Haiku 5.5 vs Ollama Offline)](#25-dual-ai-vision-engines-haiku-55-vs-ollama-offline)
   - [2.6 Claude Haiku 5.5 Economics & Prompt Caching Dashboard](#26-claude-haiku-55-economics--prompt-caching-dashboard)
   - [2.7 TA Human-in-the-Loop Arbitration & Student Regrades](#27-ta-human-in-the-loop-arbitration--student-regrades)
   - [2.8 Official College Exports (Multi-Sheet Excel & Feedback PDFs)](#28-official-college-exports-multi-sheet-excel--feedback-pdfs)
5. [Configuration & Environment Variables](#configuration--environment-variables)
6. [REST API Reference](#rest-api-reference)
7. [Testing & Quality Assurance](#testing--quality-assurance)

---

## Architectural Highlights

- **Zero Cloud Hosting Mandatory**: The entire stack (FastAPI server, SQLite WAL database, OpenCV computer vision engine, and web UIs) runs locally on an instructor's laptop.
- **Air-Gapped Operation**: Functions over a local laptop Wi-Fi hotspot in examination halls without internet connectivity.
- **Multi-TA Parallelism**: Multiple teaching assistants can connect their smartphones concurrently, claim grading stations, and upload student papers in parallel.
- **Multi-Modal Prompt Caching**: Employs Anthropic Ephemeral Prompt Caching (`cache_control: {"type": "ephemeral"}`) on questions and rubrics to slash grading costs by **90%** and cut evaluation latency by **~70%**.
- **Self-Healing Durability**: Uses SQLite Write-Ahead Logging (`PRAGMA journal_mode=WAL`), persistent rotating logs (`logs/iiserb_hub.log`), and automatic local file mirroring in `<session_dir>/exports/`.

---

## Quick-Start Guide

### Windows (1-Click)

1. Double-click [`run_hub.bat`](run_hub.bat) or run in PowerShell:
   ```powershell
   .\run_hub.ps1
   ```
2. The launcher automatically creates a virtual environment at `.\venv`, installs dependencies from `requirements.txt`, binds to all local network interfaces (`0.0.0.0:8000`), prints the mobile pairing QR code in the terminal, and launches your browser at `http://localhost:8000/scanner`.

### macOS & Linux (1-Click)

1. Open your terminal in the repository root and run:
   ```bash
   chmod +x run_hub.sh
   ./run_hub.sh
   ```
2. The script detects Python 3.10–3.13, provisions `venv`, and launches the hub.

### Docker Deployment

```bash
# Build the Docker image
docker build -t iiserb-quiz-checker .

# Run the container
docker run -p 8000:8000 \
  -v "$(pwd)/data:/app/data" \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  iiserb-quiz-checker
```

---

## Engine 1: Polyglot Coding Lab Checker Guide

Access the Digital Coding Lab Checker at `http://localhost:8000/`.

### 1.1 Supported Languages & File Ingestion

The evaluator ingests student programming assignments across **25+ languages** without requiring file renaming or manual folder reorganization:

| Family | Languages & Extensions |
| :--- | :--- |
| **Compiled Systems** | C (`.c`, `.h`), C++ (`.cpp`, `.cc`, `.cxx`, `.hpp`), Rust (`.rs`), Go (`.go`) |
| **JVM Ecosystem** | Java (`.java`), Kotlin (`.kt`), Scala (`.scala`) |
| **Scripting & Data** | Python (`.py`), Jupyter Notebooks (`.ipynb`), R (`.r`), Julia (`.jl`) |
| **Web & Node** | JavaScript (`.js`), TypeScript (`.ts`) |
| **Database & Shell** | SQL (`.sql`), Bash/Shell (`.sh`, `.bash`) |
| **Markup & Prose** | Markdown (`.md`), Plain Text (`.txt`), LaTeX (`.tex`) |

#### Supported Packaging Layouts:
- **Direct LMS ZIP Exports**: Moodle bulk assignment downloads, Google Classroom master ZIP archives.
- **Folder-per-Student**: Nested directories named by student roll number (e.g. `25101_Aarav/Q1.c`, `25102_Bhavna/Q1.c`).
- **Flat Loose Files**: Individual files matching roll numbers (e.g. `25101_q1.c`, `25102-question1.py`).
- **Jupyter Notebook Extraction**: Automatically converts code cells in `.ipynb` files into executable script streams while ignoring raw Markdown notes.

---

### 1.2 Question Paper JSON Specification

Create a JSON file defining the course code, exam title, and per-question rubrics:

```json
{
  "course_code": "DSE-201",
  "quiz_title": "Object-Oriented Programming & Data Structures Lab Quiz",
  "default_language": "c",
  "questions": [
    {
      "question_id": "Q1",
      "title": "Doubly Linked List Insertion",
      "language": "c",
      "execution_mode": "sandbox",
      "problem_statement": "Implement function `insert_sorted(Node** head, int val)` that inserts an integer into a sorted doubly linked list.",
      "topic_tags": ["linked-list", "pointers"],
      "expected_complexity": "O(N)",
      "forbidden_constructs": ["qsort", "system"],
      "required_constructs": ["malloc", "while"],
      "timeout_seconds": 2.5,
      "test_cases": [
        { "input_data": "3\n10 20 40\n25\n", "expected_output": "10 20 25 40", "is_sample": true },
        { "input_data": "1\n50\n10\n", "expected_output": "10 50", "is_sample": false }
      ]
    }
  ]
}
```

#### Field Reference:
- `execution_mode`:
  - `auto`: Executes in the sandbox if test cases are provided; falls back to static checks + LLM.
  - `sandbox`: Enforces OS-level compilation and test case execution.
  - `llm_only`: Skips compilation; grades via Claude Haiku and grading rubric (ideal for pseudocode, theory, and design).
  - `static_only`: Enforces deterministic regex, AST, and keyword checks only.
- `forbidden_constructs`: Flagged as `RULE_VIOLATION` if detected in student code.
- `required_constructs`: Must be present in the submission (e.g. `["recursion"]`).
- `test_cases`: Array of input/output pairs. If `is_sample` is `false`, it represents a hidden test case. Submissions that pass sample cases but fail hidden cases are flagged for `SAMPLE_INPUT_SPOOFING`.

---

### 1.3 Concrete Question Paper Examples

#### Example A: Full C/C++ Program (Standard In / Out)
```json
{
  "course_code": "DSE-101",
  "quiz_title": "Lab Quiz 1: Pointers & Array Reversal",
  "default_language": "c",
  "questions": [
    {
      "question_id": "Q1",
      "title": "In-Place Array Reversal",
      "language": "c",
      "execution_mode": "sandbox",
      "problem_statement": "Read integer N followed by N integers. Reverse the array in-place using two pointers and print the result separated by spaces.",
      "forbidden_constructs": ["goto"],
      "test_cases": [
        { "input_data": "5\n1 2 3 4 5\n", "expected_output": "5 4 3 2 1", "is_sample": true },
        { "input_data": "4\n-1 -5 10 0\n", "expected_output": "0 10 -5 -1", "is_sample": false }
      ]
    }
  ]
}
```

#### Example B: LeetCode-Style Function (Auto-Synthesized Driver)
When students only write a target function (e.g. `def two_sum(nums, target):` or `int is_prime(int n)`) without writing `main()` or parsing inputs, the evaluator automatically synthesizes an in-memory test harness:
```json
{
  "course_code": "DSE-201",
  "quiz_title": "Python Data Structures",
  "default_language": "python",
  "questions": [
    {
      "question_id": "Q1",
      "title": "Two Sum Problem",
      "language": "python",
      "execution_mode": "auto",
      "problem_statement": "Implement function `two_sum(nums, target)` returning zero-indexed array indices.",
      "test_cases": [
        { "input_data": "[2, 7, 11, 15]\n9\n", "expected_output": "0 1", "is_sample": true },
        { "input_data": "[3, 3]\n6\n", "expected_output": "0 1", "is_sample": false }
      ]
    }
  ]
}
```

---

### 1.4 Sandbox Execution & Driver Synthesis

- **Resource Quotas & Sandboxing by Platform**:
  - **Linux / Docker Environment**: Hard address space quotas (`RLIMIT_AS` capped at `256 MB` for C/C++ and `384 MB` for Python via `prlimit`/`setrlimit`), `RLIMIT_NPROC` process capping, blocked core dumps (`RLIMIT_CORE=0`), and POSIX `killpg` process-group isolation.
  - **Windows Host (`run_hub.bat`)**: Static AST syntax security policy bans on prohibited execution primitives (`system()`, `fork()`, `exec*()`, `popen()`, network sockets) + scrubbed environment variables + wall-clock timeout cancellation with recursive process tree termination (`taskkill /F /T`). (Note: Hard kernel address-space quotas `RLIMIT_AS` require Linux or Docker containerization).
- **Driver Synthesizer**: [`driver_synthesizer.py`](app/analyzer/driver_synthesizer.py) inspects AST function signatures, formats JSON/string test inputs into native arguments, links student functions into a harness, and asserts expected return values.

---

### 1.5 Deterministic Plagiarism & Collusion Forensics

The plagiarism engine operates on **token-level normalized syntax trees**:

```
[Raw Student Code] -> [Lexical Tokenizer] -> [Canonical Var Replacement] -> [Winnowing Fingerprints] -> [Jaccard & Shingle Matrix]
```

1. **Canonical Replacement**: Variable names are normalized to `VAR_1`, `VAR_2`, loop constructs to `LOOP`, and string literals stripped of trivial variance.
2. **Shingle Exclusivity (Rare Fingerprints)**: Standard boilerplate (e.g. `#include <stdio.h>`, standard `for` loops) is discounted. Plagiarism flags trigger only when students share rare, statistically improbable structural n-grams or identical anomalous typos.
3. **Cross-Question Collusion**: Corroborates suspicious pairs across multiple questions. If two students share matching logic across Q1, Q2, and Q3, confidence elevates to `CRITICAL_COLLUSION`.

---

### 1.6 The 4 Triage Queues (GREEN, BLUE, RED, GRAY)

```
+-----------------------------------------------------------------------------------------+
|  Queue      | Description                                                               |
+-------------+---------------------------------------------------------------------------+
|  🟢 GREEN   | Clean, textbook solution passing all sample and hidden test cases.        |
|  🔵 BLUE    | Correct solution with an innovative, distinctive, or advanced algorithm.  |
|  🔴 RED     | Compile error, infinite loop, failed test cases, rule breach, or clone.   |
|  ⚪ GRAY    | Non-executable prose or unattempted question requiring manual TA review.  |
+-----------------------------------------------------------------------------------------+
```

---

## Engine 2: Physical Paper Exam Checker & Grading Hub Guide

Access the Physical Paper Scanner & Hub at `http://localhost:8000/scanner`.

---

### 2.1 Dual Exam Paper Formats

The hub supports two primary physical examination modes relevant to university evaluation:

1. **Combined Question + Answer Sheet (`COMBINED_QUESTION_ANSWER`)**:
   - The question statement, sub-parts, and dedicated answer boxes are printed together on the same physical pages.
   - Ideal for mid-semester quizzes, continuous assessments, and tutorial tests.
2. **Separate Question Booklet + Answer Sheets (`SEPARATE_QUESTION_BOOKLET`)**:
   - The question paper is distributed as a standalone sheet. Students write their answers in standardized answer books.
   - Ideal for comprehensive end-semester exams.

---

### 2.2 Printable Exam Booklet PDF Generation

Before the exam, faculty can generate and print official departmental answer booklets:

1. Open the Hub Setup tab (`Step 1`).
2. Configure Course Code, Exam Name, Total Marks, and Question Space boundaries (`Y1%` to `Y2%`).
3. Click **📄 Printable Exam Booklet PDF**.
4. The hub generates a print-ready A4 PDF with:
   - **4 ArUco Corner Markers (IDs 0, 1, 2, 3)**: Positioned at $(0,0)$, $(W,0)$, $(W,H)$, and $(0,H)$ to enable millimeter-level geometric perspective rectification on smartphone cameras.
   - **Header QR Code**: Encodes Course Code, Exam Name, and Date.
   - **Structured Question Answer Boxes**: Clear bounding boxes designating boundaries for each question.

---

### 2.3 Exam Hall Ingestion & Mobile Camera Scanner

In the examination hall, TAs scan student papers using their smartphone cameras:

```
[Laptop Running Hub] <--- Local Wi-Fi / Hotspot ---> [TA Smartphone 1]
                                                ---> [TA Smartphone 2]
                                                ---> [TA Smartphone 3]
```

1. **Connect to Wi-Fi**: Turn on Mobile Hotspot on the instructor's laptop. TAs connect to the hotspot (no internet required).
2. **Scan Pairing QR**: TAs scan the QR code displayed in the laptop terminal or topbar.
3. **Teaching Assistant Verification**: The TA enters their name and assigned station (e.g. *TA: Aarav · Station: Hall A - Rows 1-5*).
4. **Real-Time Edge Detection & Auto-Snap**:
   - The smartphone viewfinder displays a green alignment box.
   - When the paper is held steady and sharp focus is detected, the camera **auto-snaps** the page without button presses.
5. **25-Page Visual Continuity Slots**:
   - Numbered slots (`1` through `25`) indicate scanned pages. Supplementary sheets are automatically marked as `Extra Sheet 1`, `Extra Sheet 2`.
6. **Air-Gapped Offline IndexedDB Buffering**:
   - If Wi-Fi momentarily drops, photos buffer in smartphone IndexedDB storage and auto-sync to the laptop upon reconnection.

---

### 2.4 Computer Vision Rectification & Handwriting Sharpening

When an image reaches the laptop, [`image_processor.py`](app/analyzer/image_processor.py) processes it through a deterministic CV pipeline:

```
[Raw Photo] -> [ArUco / Quad Warp] -> [Orientation Fix] -> [Illumination Flattening] -> [CLAHE Contrast] -> [Question Crops]
```

1. **Perspective Flattening**: Locates corner quad coordinates and executes `cv2.warpPerspective` to flatten desk angle distortions to canonical A4 dimensions ($1654 \times 2338$ px).
2. **Auto-Orientation Correction**: Detects upside-down or sideways pages ($90^\circ$, $180^\circ$, $270^\circ$) using gradient text density and rotates the sheet upright.
3. **Illumination Flattening & Shadow Division**: Downsamples luminance by $4\times$, estimates background lighting gradient, and divides out shadows caused by smartphone bodies or hall lighting.
4. **CLAHE & Unsharp Masking**: Enhances faint pencil and blue/black ink strokes while preserving paper texture.
5. **Perceptual Hash Duplicate Detection**: Uses Hamming distance on 64-bit image hashes. If a TA retakes a page accidentally, the system retains the sharper frame automatically.
6. **Visual Identity Stamping**: Stamps each cropped question answer with a header (`STUDENT: {roll_no} | QUESTION: {q_id} | MAX: {max_marks}M`) so batched AI calls never mix up student identities.

---

### 2.5 Dual AI Vision Engines (Haiku 5.5 vs Ollama Offline)

The hub supports dynamic switching between Cloud AI and 100% Offline AI:

| Feature | Anthropic Claude Haiku 5.5 | Ollama / Qwen2.5-VL-7B |
| :--- | :--- | :--- |
| **Connectivity** | Cloud API (Requires Internet) | **100% Offline (Air-Gapped Hall)** |
| **Economics** | **90% Discount** via Ephemeral Prompt Caching | Free & Local (Uses Laptop GPU/CPU) |
| **Speed** | 1.1s per batch (5 crops) | Depends on laptop hardware |
| **Activation** | Select `claude-haiku-5-5` | Select `qwen2.5-vl-7b-instruct` |

Switching models in the UI automatically synchronizes `vlm_provider` between `anthropic`, `ollama`, and `openai`.

---

### 2.6 Claude Haiku 5.5 Economics & Prompt Caching Dashboard

Click **⚡ Haiku 5.5 Usage** or the topbar VLM status badge (`🤖 VLM: Claude`) to open the real-time financial ledger:

- **Prompt Caching Economics**:
  - Base Input: `$0.80 / MTok`
  - Cache Read (90% discount): **`$0.08 / MTok`**
  - Cache Write: `$1.00 / MTok`
  - Output: `$4.00 / MTok`
- **KPI Metrics Displayed**:
  - Total Actual Cost in USD (`$`) and INR (`₹`)
  - Gross Standard Cost (without prompt caching)
  - Net Savings in USD and Percentage
  - Effective Cache Hit Rate (typically **85%–94%**)
  - Mean Latency per Call & Error Counts
- **Scope Breakdown**: Shows active exam session consumption vs lifetime global laptop usage.

---

### 2.7 TA Human-in-the-Loop Arbitration & Student Regrades

Faculty and TAs maintain complete authority over final grades:

1. **1-Click Batch Approval**: Approves all `GREEN` (high confidence $\ge 85\%$) answers with a single click.
2. **Interactive Student Inspector**:
   - Inspects high-resolution question crops with pan and zoom.
   - Adjusts awarded marks with live total recalculation.
   - Adds TA comments exported directly into official gradebook sheets.
   - Keyboard shortcuts: `J` / `Down` for next student, `K` / `Up` for previous student, `A` to approve.
3. **Student Regrade Petitions**:
   - TAs can lodge an appeal on behalf of a student, or students can petition specific step scoring.
   - Approving an appeal automatically updates the student's marks in SQLite, updates the triage badge, and recalculates the final gradebook.

---

### 2.8 Official College Exports (Multi-Sheet Excel & Feedback PDFs)

1. **Multi-Sheet Excel Gradebook (`.xlsx`)**:
   - **Sheet 1 (`Student Marks Summary`)**: Roll Number, Student Name, Pages Scanned, Per-Question Marks (`Q1..Qk`), Total Awarded, Maximum Marks, Percentage, Triage Badge, Review State, and Combined Feedback.
   - **Sheet 2 (`Detailed Question Breakdown`)**: Question-by-question confidence scores, legibility metrics, handwritten OCR transcriptions, and individual TA comments.
   - **Formula Injection Guard**: Automatically neutralizes formula injection attempts (`=`, `+`, `-`, `@`) in student names or OCR strings.
2. **Student Feedback PDFs (`.zip`)**:
   - Downloads a ZIP archive of individual annotated PDFs displaying question crops, step scores, rubrics, and TA notes.
3. **Automatic Local Mirroring**:
   - All Excel workbooks and feedback ZIP archives are automatically mirrored to `<exam_storage_dir>/exports/` on the laptop to prevent loss from browser download blockers.

---

## Configuration & Environment Variables

Configure settings via `.env` or system environment variables:

```ini
# --- Server & Network ---
HUB_PORT=8000
ALLOWED_ORIGINS=*
REQUIRE_AUTH=false
API_AUTH_TOKEN=

# --- Paper Scanner & Storage ---
PAPER_STORAGE_DIR=./data/paper_scans
SQLITE_DB_PATH=./data/hitl_calibration.db
MAX_PAGES_PER_STUDENT=25
MAX_STUDENTS_PER_SESSION=500

# --- AI Vision Model & Provider ---
VLM_PROVIDER=anthropic             # "anthropic", "ollama", "openai", "local"
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-haiku-5-5
FALLBACK_MODEL=claude-haiku-4-5-20251001

# --- Offline Ollama VLM Settings ---
LOCAL_VLM_URL=http://localhost:11434/v1
LOCAL_VLM_MODEL=qwen2.5-vl:7b
LOCAL_VLM_API_KEY=ollama

# --- AI Concurrency & Batching ---
LLM_CONCURRENCY=6                  # Parallel worker tasks
LLM_VISION_BATCH_SIZE=5            # Question crops per cached LLM call
AUTO_APPROVE_CONFIDENCE=0.85       # Threshold for GREEN auto-approval

# --- Digital Code Sandbox Limits ---
GCC_TIMEOUT_SECONDS=3.0
COMPILE_TIMEOUT_SECONDS=10.0
CLONE_THRESHOLD_PERCENT=88.0
```

---

## REST API Reference

The hub exposes a comprehensive REST API:

### Paper Scanner & Session Management
- `GET /api/hub-info`: Returns local Wi-Fi IPs, active storage folder, disk statistics, and VLM provider status.
- `GET /api/sessions`: Lists saved exam sessions on the laptop.
- `POST /api/sessions`: Creates a new exam session.
- `GET /api/sessions/{session_id}`: Retrieves full session state, student roster, and evaluation status.
- `DELETE /api/sessions/{session_id}`: Permanently deletes an exam session.
- `POST /api/booklet/preview.pdf`: Generates an instant printable A4 exam booklet template preview.
- `GET /api/sessions/{session_id}/template.pdf`: Generates printable exam booklet PDF for an active session.

### Page Upload & Ingestion
- `POST /api/upload-page`: Ingests a single scanned page image (raw bytes or multipart).
- `POST /api/upload-chunk`: Uploads a 64 KB binary chunk for low-bandwidth mobile connections.
- `POST /api/stitch`: Assembles chunks and triggers image processing.
- `POST /api/sessions/{session_id}/finalize-student`: Finalizes student scanning and marks pages ready for evaluation.

### AI Grading & Arbitration
- `POST /api/sessions/{session_id}/evaluate`: Triggers concurrent prompt-cached Claude Haiku or Ollama grading.
- `POST /api/sessions/{session_id}/bulk-approve-green`: Approves all high-confidence green evaluations.
- `POST /api/sessions/{session_id}/override`: Applies TA manual marks override and feedback comment.
- `POST /api/sessions/{session_id}/students/{roll_no}/regrade-request`: Lodges a student regrade petition.
- `POST /api/sessions/{session_id}/regrade-requests/{id}/resolve`: Resolves a regrade petition and updates marks.

### Telemetry & Economics
- `GET /api/haiku-usage`: Retrieves token telemetry, cache hit rates, and financial ledger data.
- `POST /api/haiku-usage/model`: Switches active primary VLM model and synchronizes provider.
- `POST /api/haiku-usage/reset`: Resets global token counters.
- `GET /api/vlm-status`: Returns active VLM engine status (`anthropic`, `ollama`, `openai`).
- `POST /api/runtime-settings`: Updates runtime concurrency, model, and storage directory.

### Exports
- `GET /api/sessions/{session_id}/export.xlsx`: Downloads clean multi-sheet Excel gradebook.
- `GET /api/sessions/{session_id}/export-feedback-zip`: Downloads ZIP archive of annotated student PDFs.
- `GET /api/sessions/{session_id}/analytics`: Returns score distribution, grade histogram, and question statistics.

---

## Testing & Quality Assurance

The repository includes a comprehensive automated test suite covering digital coding lab evaluation, paper scanning redundancy, ArUco detection, prompt caching economics, and student regrades:

```bash
# Run the complete test suite
pytest tests/ -v

# Run paper exam checker tests specifically
pytest tests/test_haiku_usage_and_regrades.py tests/test_paper_redundancies.py -v

# Run polyglot coding lab checker tests
pytest tests/test_pipeline_logic_fixes.py tests/test_v6_roadmap_features.py -v
```

All **290 automated unit and integration tests** pass with zero regressions.

---

## License & Attribution

Developed for the **Department of Data Science & Engineering, IISER Bhopal**.  
Licensed under the **Apache License 2.0**.
