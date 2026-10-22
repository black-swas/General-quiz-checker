#!/usr/bin/env python3
"""100% Laptop-Hosted Mobile Exam Scanner & AI Grading Hub (`local_hub.py`).

No cloud hosting (Render / Vercel) required! Your laptop hosts both:
1. The Mobile Camera Scanner App (`/scan`) for TA smartphones over Local Wi-Fi / Hotspot.
2. The Laptop Command Center & Live Review Dashboard (`/scan?view=laptop` and `/`) for real-time scan monitoring,
   folder selection, concurrent Prompt-Cached `claude-haiku-5-5` grading, and multi-sheet Excel (`.xlsx`) export.

Usage:
    python3 local_hub.py --port 8000 --open-browser
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import cv2
import uvicorn

from app.config import settings
from app.main import detect_local_wifi_ips

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _find_openssl_bin() -> str:
    which_bin = shutil.which("openssl")
    if which_bin:
        return which_bin
    for candidate in [
        r"C:\Program Files\Git\usr\bin\openssl.exe",
        r"C:\msys64\usr\bin\openssl.exe",
        r"C:\Program Files (x86)\Git\usr\bin\openssl.exe",
    ]:
        if os.path.isfile(candidate):
            return candidate
    return "openssl"


def _generate_self_signed_cert(cert_dir: Path, ip_addr: str) -> tuple[Path, Path]:
    """Generate a local self-signed TLS certificate with SubjectAltName for the laptop Wi-Fi IP."""
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_file = cert_dir / "hub_cert.pem"
    key_file = cert_dir / "hub_key.pem"
    if cert_file.is_file() and key_file.is_file():
        return cert_file, key_file

    san = f"IP:{ip_addr},IP:127.0.0.1,DNS:localhost"
    openssl_bin = _find_openssl_bin()
    cmd = [
        openssl_bin, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key_file),
        "-out", str(cert_file),
        "-days", "365",
        "-subj", f"/CN={ip_addr}/O=IISERB-Quiz-Hub",
        "-addext", f"subjectAltName={san}",
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cert_file, key_file


def _render_ascii_qr(url: str) -> str:
    """Render a compact terminal QR code using Unicode half-blocks (▀ ▄ █) via OpenCV's QRCodeEncoder."""
    try:
        encoder = cv2.QRCodeEncoder_create()
        mat = encoder.encode(url)
        h, w = mat.shape[:2]
        lines = []
        for r in range(0, h, 2):
            row_chars = ["    "]
            for c in range(w):
                top_black = mat[r, c] == 0
                bot_black = (mat[r + 1, c] == 0) if (r + 1 < h) else False
                if top_black and bot_black:
                    row_chars.append("█")
                elif top_black and not bot_black:
                    row_chars.append("▀")
                elif not top_black and bot_black:
                    row_chars.append("▄")
                else:
                    row_chars.append(" ")
            lines.append("".join(row_chars))
        return "\n".join(lines)
    except Exception:
        return ""


def _print_banner(scheme: str, ips: list[str], port: int, storage_dir: str) -> None:
    scanner_url = f"{scheme}://{ips[0]}:{port}/scan"
    laptop_url = f"{scheme}://{ips[0]}:{port}/scan?view=laptop"
    line = "=" * 78
    qr_art = _render_ascii_qr(scanner_url)
    print(f"\n{line}")
    print("  IISER BHOPAL — 100% LAPTOP-HOSTED EXAM SCANNER & AI GRADING HUB (v5.1)")
    print(line)
    print(f"  📱 Mobile Scanner Link (TA Phones) : {scanner_url}")
    print(f"  💻 Laptop Live Command Center      : {laptop_url}")
    if len(ips) > 1:
        print(f"  🌐 Alternate Wi-Fi / Hotspot IPs   : {', '.join(f'{scheme}://{ip}:{port}/scan' for ip in ips[1:])}")
    vlm_mode = "100% Offline (Ollama / Local)" if settings.vlm_provider in ("ollama", "local") else f"Cloud ({settings.vlm_provider.capitalize()})"
    print(f"  🤖 AI VLM Engine                   : {settings.primary_model} [{vlm_mode}] ({settings.llm_concurrency} workers, batch={settings.llm_vision_batch_size}, Prompt Caching ON)")
    print(line)
    if qr_art:
        print("  SCAN THIS QR CODE WITH THE TA SMARTPHONE CAMERA TO OPEN THE SCANNER:")
        print(qr_art)
        print(line)
    print("  TIP: You can change the storage folder, API key, and concurrency anytime")
    print(f"       inside the Laptop Command Center at {laptop_url}")
    print(f"{line}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the 100% Laptop-Hosted Mobile Exam Scanner & AI Grading Hub.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0 for all Wi-Fi interfaces)")
    parser.add_argument("--port", type=int, default=settings.hub_port, help="Port number (default: 8000)")
    parser.add_argument("--https", action="store_true", help="Enable HTTPS with auto-generated local TLS certificate for mobile WebRTC camera")
    parser.add_argument("--storage-dir", default=settings.paper_storage_dir, help="Folder on laptop to store scanned student papers")
    parser.add_argument("--concurrency", type=int, default=settings.llm_concurrency, help="Concurrent Claude API workers (default: 6)")
    parser.add_argument("--batch-size", type=int, default=settings.llm_vision_batch_size, help="Question crops per Claude call (default: 5)")
    parser.add_argument("--open-browser", action="store_true", help="Automatically open the Laptop Command Center in your default browser")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development")
    args = parser.parse_args()

    os.environ["HUB_PORT"] = str(args.port)
    os.environ["PAPER_STORAGE_DIR"] = str(args.storage_dir)
    os.environ["LLM_CONCURRENCY"] = str(args.concurrency)
    os.environ["LLM_VISION_BATCH_SIZE"] = str(args.batch_size)
    settings.hub_port = args.port
    settings.paper_storage_dir = str(args.storage_dir)
    settings.llm_concurrency = args.concurrency
    settings.llm_vision_batch_size = args.batch_size

    ips = detect_local_wifi_ips()
    ssl_kwargs = {}
    scheme = "http"

    if args.https:
        try:
            cert_file, key_file = _generate_self_signed_cert(Path("./data/certs"), ips[0])
            ssl_kwargs = {"ssl_certfile": str(cert_file), "ssl_keyfile": str(key_file)}
            scheme = "https"
        except Exception as exc:
            print(f"[WARN] Could not generate self-signed HTTPS certificate ({exc}); falling back to HTTP.", file=sys.stderr)

    _print_banner(scheme, ips, args.port, args.storage_dir)

    if args.open_browser:
        laptop_url = f"{scheme}://localhost:{args.port}/scan?view=laptop"
        threading.Timer(1.2, lambda: webbrowser.open(laptop_url)).start()

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        **ssl_kwargs,
    )


if __name__ == "__main__":
    main()
