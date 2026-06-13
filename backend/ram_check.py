"""
backend/ram_check.py
====================
Stage 3 RAM-budget verification. The live system is exactly two processes —
Ollama + FastAPI — so the laptop must hold the 3B chat model resident with
headroom for the app and the OS.

This script:
  1. reports total / available RAM (psutil),
  2. checks the Ollama server is up and both required models are pulled,
  3. prints a pass/fail verdict.

Exit code is non-zero if a CRITICAL check fails (Ollama down, a required model
missing, or available RAM below the hard floor) so launch/start.bat can gate on it.

Usage:
    python backend/ram_check.py
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

# Budget (GiB). llama3.2:3b ~2.0 GB resident + nomic-embed-text ~0.3 GB +
# FastAPI/Python + OS. Floor = refuse to run; recommended = comfortable headroom.
AVAILABLE_FLOOR_GB = 3.0
AVAILABLE_RECOMMENDED_GB = 5.0

GIB = 1024 ** 3


def gib(n_bytes: int) -> float:
    return n_bytes / GIB


def main() -> None:
    try:
        import psutil
    except ImportError:
        sys.exit("ERROR: psutil not installed. Run: pip install psutil")

    # Import here so the module is testable even if backend/ isn't on sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ollama_client import OLLAMA, MODEL_CHAT, MODEL_EMBED, verify_models

    print("=" * 60)
    print("CSEC AI Study Partner — Stage 3 RAM / Ollama check")
    print("=" * 60)

    vm = psutil.virtual_memory()
    total_gb = gib(vm.total)
    avail_gb = gib(vm.available)
    print(f"RAM total      : {total_gb:5.1f} GiB")
    print(f"RAM available  : {avail_gb:5.1f} GiB")
    print(f"  floor        : {AVAILABLE_FLOOR_GB:5.1f} GiB (hard minimum)")
    print(f"  recommended  : {AVAILABLE_RECOMMENDED_GB:5.1f} GiB")

    critical: list[str] = []
    warnings: list[str] = []

    if avail_gb < AVAILABLE_FLOOR_GB:
        critical.append(
            f"available RAM {avail_gb:.1f} GiB is below the {AVAILABLE_FLOOR_GB} GiB floor"
        )
    elif avail_gb < AVAILABLE_RECOMMENDED_GB:
        warnings.append(
            f"available RAM {avail_gb:.1f} GiB is below the recommended "
            f"{AVAILABLE_RECOMMENDED_GB} GiB — close other apps before a session"
        )

    print(f"\nOllama         : {OLLAMA}")
    info = verify_models()
    if not info["healthy"]:
        critical.append("Ollama server is not reachable — start Ollama and retry")
        print("  status       : DOWN")
    else:
        print("  status       : up")
        print(f"  models pulled: {', '.join(info['pulled']) or '(none)'}")
        if info["missing"]:
            critical.append(
                "missing models: " + ", ".join(info["missing"])
                + f" — run: ollama pull {MODEL_CHAT}  /  ollama pull {MODEL_EMBED}"
            )

    print("\n" + "=" * 60)
    for w in warnings:
        print(f"  WARN     : {w}")
    if critical:
        for c in critical:
            print(f"  CRITICAL : {c}")
        print("RESULT: FAIL")
        sys.exit(1)
    print("RESULT: PASS — system is within budget and ready.")


if __name__ == "__main__":
    main()
