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
from backend.ingest_v2.adapters.generic_office import GenericOfficeAdapter
from backend.ingest_v2.adapters.generic_pdf import GenericPDFAdapter


def wire_adapters(enable_office_adapter: bool = False) -> None:
    """Register the production adapter set, in dispatch order.

    GenericOfficeAdapter is included ONLY when enable_office_adapter is True (a
    per-subject opt-in read from the manifest). It sits AFTER MoESLMSAdapter -- so a
    .docx/.pptx under Notes\\T&T MoE SLMS is claimed by MoESLMSAdapter first -- and
    before GenericPDFAdapter, which stays last as the .pdf catch-all. A subject that
    leaves the flag False (e.g. POB) never has the Office adapter in its dispatch, so
    its loose Office files remain unclaimed exactly as before (test_pob_parity)."""
    order = [CaribbeanAIAdapter, MoESLMSAdapter, KerwinMCQAdapter]
    if enable_office_adapter:
        order.append(GenericOfficeAdapter)
    order.append(GenericPDFAdapter)
    register_adapters(*order)
