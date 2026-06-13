"""
backend/ram_check.py
====================
Stage 3 RAM-budget verification. The live system is exactly two processes -
Ollama + FastAPI - so the laptop must hold the 3B chat model resident with
headroom for the app and the OS.

This script:
  1. reports total / available RAM (psutil),
  2. checks the Ollama server is up and both required models are pulled,
  3. prints an advisory verdict (PASS or WARN).

This check is ADVISORY ONLY - it never FAILs and never blocks Stage 3. A RAM
snapshot taken while other tools (e.g. an editor or Claude Code itself) are
resident is not a real test of whether a study session will run. The real test
is whether a session runs without freezing. So low RAM is surfaced as a warning
to close background apps, not as a hard gate. Exit code is always 0.

Usage:
    python backend/ram_check.py
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

# Advisory RAM thresholds (GiB). llama3.2:3b ~2.0 GB resident + nomic-embed-text
# ~0.3 GB + FastAPI/Python + OS. None of these block - they only tune the warning.
#   >= 3.5 GiB        : PASS (comfortable headroom)
#   2.0 - 3.5 GiB     : WARN - close background apps before studying
#   < 2.0 GiB         : WARN - close background apps, do not start a session yet
# AVAILABLE_FLOOR_GB is the documented absolute minimum; it is informational only.
AVAILABLE_FLOOR_GB = 1.0
AVAILABLE_LOW_GB = 2.0
AVAILABLE_RECOMMENDED_GB = 3.5

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
    print("CSEC AI Study Partner - Stage 3 RAM / Ollama check")
    print("=" * 60)

    vm = psutil.virtual_memory()
    total_gb = gib(vm.total)
    avail_gb = gib(vm.available)
    print(f"RAM total      : {total_gb:5.1f} GiB")
    print(f"RAM available  : {avail_gb:5.1f} GiB")
    print(f"  abs. minimum : {AVAILABLE_FLOOR_GB:5.1f} GiB (informational only)")
    print(f"  recommended  : {AVAILABLE_RECOMMENDED_GB:5.1f} GiB")

    warnings: list[str] = []

    # Tiered RAM advisory - never fatal.
    if avail_gb >= AVAILABLE_RECOMMENDED_GB:
        pass  # comfortable headroom
    elif avail_gb >= AVAILABLE_LOW_GB:
        warnings.append(
            f"available RAM {avail_gb:.1f} GiB is below the recommended "
            f"{AVAILABLE_RECOMMENDED_GB} GiB - close background apps before studying"
        )
    else:
        warnings.append(
            f"available RAM {avail_gb:.1f} GiB is below {AVAILABLE_LOW_GB} GiB - "
            "close background apps, do not start a session yet"
        )

    print(f"\nOllama         : {OLLAMA}")
    info = verify_models()
    if not info["healthy"]:
        warnings.append("Ollama server is not reachable - start Ollama before a session")
        print("  status       : DOWN")
    else:
        print("  status       : up")
        print(f"  models pulled: {', '.join(info['pulled']) or '(none)'}")
        if info["missing"]:
            warnings.append(
                "missing models: " + ", ".join(info["missing"])
                + f" - run: ollama pull {MODEL_CHAT}  /  ollama pull {MODEL_EMBED}"
            )

    print("\n" + "=" * 60)
    for w in warnings:
        print(f"  WARN     : {w}")
    if warnings:
        print("RESULT: WARN - advisory only; the real test is a session that runs without freezing.")
    else:
        print("RESULT: PASS - system is within budget and ready.")


if __name__ == "__main__":
    main()
