# PHASE: build
"""
backend/ingest_v2/registry.py
=============================
Wires the concrete adapters into the orchestrator's dispatch order. Kept separate
from orchestrator.py so the orchestrator has no hard dependency on any specific
adapter (and so tests can register a custom set).

Dispatch order matters: GenericPDFAdapter is the catch-all and MUST be last, so a
.pdf inside a specialised folder (e.g. T&T MoE SLMS) is claimed by its specific
adapter before the generic one sees it.

"""

from backend.ingest_v2.orchestrator import register_adapters
from backend.ingest_v2.adapters.caribbean_ai import CaribbeanAIAdapter
from backend.ingest_v2.adapters.moe_slms import MoESLMSAdapter
from backend.ingest_v2.adapters.kerwin_mcq import KerwinMCQAdapter
from backend.ingest_v2.adapters.generic_pdf import GenericPDFAdapter


def wire_adapters() -> None:
    """Register the production adapter set, in dispatch order. GenericPDFAdapter is
    last (the catch-all) so a .pdf inside T&T MoE SLMS is claimed by MoESLMSAdapter
    first."""
    register_adapters(
        CaribbeanAIAdapter,
        MoESLMSAdapter,
        KerwinMCQAdapter,
        GenericPDFAdapter,
    )
