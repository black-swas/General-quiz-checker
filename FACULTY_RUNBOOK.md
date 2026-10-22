# 🎓 IISER Bhopal — Faculty & TA Exam Operations Runbook
## 100% Laptop-Hosted Physical Paper Scanner, AI Evaluator & Gradebook Hub

---

### Executive Overview

This system provides a **100% laptop-hosted examination management and paper grading pipeline** designed specifically for university exam conditions (e.g., IISER Bhopal Data Science & Engineering courses).

- **Zero Cloud Reliance**: The entire web server, database, computer vision pipeline, and export engine run directly on the instructor's laptop.
- **Air-Gapped Hall Operation**: Works seamlessly over a local Wi-Fi router or laptop mobile hotspot without requiring an active internet connection.
- **Multi-TA Concurrent Scanning**: Multiple teaching assistants can connect their smartphones simultaneously to ingest student answer sheets in parallel.
- **Computer Vision Enhancement**: Real-time ArUco corner alignment, automatic perspective desk-border cropping, upside-down auto-rotation, and contrast enhancement.
- **Dual AI Grading Engines**:
  1. **Anthropic Claude Haiku 5.5** with Ephemeral Prompt Caching (90% discount on cached questions and rubrics).
  2. **Ollama / Qwen2.5-VL-7B** for 100% offline, air-gapped AI vision evaluation.
- **Human-in-the-Loop Governance**: 4-tier triage queues (`GREEN` high confidence, `BLUE` medium, `RED` needs review, `GRAY` pending), 1-click batch approvals, manual mark overrides, and student regrade petition arbitration.
- **Official College Deliverables**: Formula-injection-safe multi-sheet Excel gradebook (`.xlsx`) and annotated student feedback PDFs (`.zip`).

---

### Quick Start Checklist

```
+--------------------------------------------------------------------------------+
|  1. Double-click `run_hub.bat` (Windows) or `./run_hub.sh` (macOS/Linux)        |
|  2. Turn on Laptop Mobile Hotspot (or connect laptop to classroom Wi-Fi)       |
|  3. Open Laptop Command Center in browser (http://localhost:8000/scanner)       |
|  4. TAs scan terminal / topbar QR code with smartphone cameras to connect      |
|  5. TAs enter their Name & Station to begin high-speed scanning                |
+--------------------------------------------------------------------------------+
```

---

### Detailed Operational Workflow

#### Phase 1: Pre-Exam Preparation (1–2 Days Before Exam)

1. **Launch the Hub**:
   - **Windows**: Double-click [`run_hub.bat`](file:///run_hub.bat) or run `powershell -ExecutionPolicy Bypass -File .\run_hub.ps1`.
   - **macOS / Linux**: Run `./run_hub.sh` in the terminal.
   - The hub auto-detects Python, prepares the virtual environment, detects local Wi-Fi IPs, prints an ASCII QR code, and opens the Command Center in your default browser.

2. **Configure Exam Metadata (Step 1)**:
   - **Course Code**: e.g., `DSE-201`, `DSE-302`, `CS-101`.
   - **Exam Name**: e.g., `Mid-Sem Written Examination`.
   - **Exam Date**: Date of the examination.
   - **Total Marks**: Overall exam mark ceiling (e.g., `50`).
   - **Paper Type**:
     - *Combined Question + Answer Sheet*: Single booklet where questions and answer spaces are printed together.
     - *Separate Question Booklet + Answer Sheet*: Questions provided separately; students write in standard answer sheets.
   - **Pages per Student**: Number of expected physical pages (1 to 25).

3. **Configure Question Coordinates & Rubrics**:
   - Add each question space with its Question ID (`Q1`, `Q2`, ...), page number, Y-coordinate percentage boundaries (`Y1%` to `Y2%`), weight in marks, and grading rubric.
   - Alternatively, load sample exam templates via the **"Load a sample paper..."** dropdown or import a pre-saved Question Paper JSON.

4. **Generate Printable Exam Booklets**:
   - Click **📄 Printable Exam Booklet PDF**.
   - The hub immediately generates an A4 PDF template featuring:
     - 4 high-precision corner ArUco fiducial markers for real-time edge alignment.
     - Header QR code containing Course Code, Exam Name, and Date.
     - Cleanly demarcated handwritten answer zones.
   - Send this PDF to the departmental printer for student copies.

5. **Import Student Roster**:
   - Click **📋 Import Roster CSV** to upload the official class roll sheet (`roll_no,student_name`).

---

#### Phase 2: Exam Day Hall Ingestion (During / Immediately After Exam)

1. **Network Setup**:
   - **Option A (Air-Gapped Hotspot)**: Turn on "Mobile Hotspot" on the laptop (Windows Settings -> Network -> Mobile Hotspot). No SIM card or internet required!
   - **Option B (Classroom Wi-Fi)**: Connect laptop and TA smartphones to the departmental Wi-Fi network.

2. **TA Smartphone Connection**:
   - Teaching assistants connect to the laptop's Wi-Fi / Hotspot.
   - TAs open their smartphone camera and scan the QR code displayed in the laptop terminal or topbar.
   - The TA enters their Name and Station ID (e.g., *TA: Ananya · Row A*) to claim their grading station.

3. **Scanning & Visual Continuity Tracking**:
   - **Auto-Snap**: The mobile viewfinder tracks real-time paper edges. When held steady within the green guide box and sharp focus is achieved, the photo snaps automatically.
   - **Manual Shutter**: Tap **📸 Snap Page** or use Bluetooth volume keys.
   - **Multi-Page Continuity**: Visual page indicator slots (1 to 25) prevent missed pages. Extra supplementary sheets are automatically indexed as `Extra Sheet 1`, `Extra Sheet 2`.
   - **Offline Resilience**: If Wi-Fi momentarily drops in a remote corner of the hall, photos buffer securely in smartphone IndexedDB and auto-sync to the laptop upon reconnection.

---

#### Phase 3: AI-Assisted Evaluation

1. **Model Selection**:
   - **Cloud Mode (Recommended)**: `claude-haiku-5-5` via Anthropic API with Ephemeral Prompt Caching. Caches questions and rubrics with a **90% discount** ($0.08 / MTok cache reads).
   - **100% Air-Gapped Mode**: `qwen2.5-vl-7b-instruct` running via local Ollama daemon (`http://localhost:11434/v1`). Zero packets leave the laptop.
2. **Execute Evaluation**:
   - In Step 3 (Live Review & AI Grading), click **🚀 Run AI Vision Evaluation**.
   - The laptop crops all student answers, applies unsharp contrast filtering, stamps visual student identity headers, and evaluates all submissions in parallel.
   - Click **⚡ Haiku 5.5 Usage** to inspect live cost accounting, token savings, and cache hit rates.

---

#### Phase 4: Human-in-the-Loop TA Verification & Arbitration

1. **4-Tier Queue Triage**:
   - 🟢 **GREEN (Auto-Approved / High Confidence)**: Clear, unambiguous handwriting with high AI confidence (>= 85%).
   - 🔵 **BLUE (Review Recommended)**: Legible answers with partial credit decisions or medium confidence.
   - 🔴 **RED (Mandatory TA Attention)**: Ambiguous calculations, crossed-out work, or low confidence.
   - ⚪ **GRAY (Pending / Empty)**: Unanswered questions or unsubmitted sheets.

2. **TA Arbitration**:
   - Click **✓ Approve Green (High Confidence)** to approve all green submissions in one click.
   - TAs use the **Student Inspector** to zoom into answer crops, verify mathematical reasoning, adjust marks, and enter comments.
   - Keyboard shortcuts: `J` / `Down` for next student, `K` / `Up` for previous, `A` to Approve.
   - Click **⚖️ Student Regrades** to resolve student regrade petitions. Approving an appeal automatically updates marks in the database and gradebook.

---

#### Phase 5: Exporting & Departmental Record Keeping

1. **Download Gradebook**:
   - Click **📊 Export Multi-Sheet Excel (.xlsx)**.
   - Sheet 1: Roll No, Name, Per-Question Marks, Total, Percentage, and Status.
   - Sheet 2: Detailed question-by-question confidence scores, legibility metrics, OCR transcriptions, and TA feedback.
   - Formula-injection protected (`_safe_excel_cell`).
2. **Download Feedback Archives**:
   - Click **📦 Export Feedback PDFs (ZIP)** to download annotated student PDFs with mark breakdowns and rubrics.
3. **Automatic Local Archival**:
   - All exported Excel and ZIP files are automatically mirrored to `<exam_storage_dir>/exports/` on the laptop.
4. **Persistent Server Logs**:
   - Operational logs are saved to `logs/iiserb_hub.log` with automatic 10 MB rotation (up to 5 backup files).

---

### Troubleshooting & Technical FAQ

| Issue | Cause | Solution |
| :--- | :--- | :--- |
| **Mobile camera blocked on phone** | Browser security blocks WebRTC on non-HTTPS IP addresses | 1. Use the built-in **📁 File Upload** button on the mobile UI (supports live camera snap natively).<br>2. Or launch hub with `--https` (`run_hub.bat --https`). |
| **TA phone cannot reach laptop IP** | Windows Firewall or AP Isolation | 1. In Windows Defender Firewall, allow Python through Private Networks.<br>2. Use Laptop Mobile Hotspot to avoid college router AP client isolation. |
| **Local Ollama VLM not responding** | Ollama service not running | Run `ollama serve` and `ollama run qwen2.5-vl:7b` in a separate terminal. |
| **Accidental page retake** | Duplicate photo scanned | System detects identical perceptual image hashes and retains the sharper frame automatically. |
| **Terminal closed by accident** | Server process terminated | Re-run `run_hub.bat`. All sessions, student photos, and grades are safe in SQLite (`data/hitl_calibration.db`) with WAL durability. |
