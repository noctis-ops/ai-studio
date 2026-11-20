"""fal.ai job adapter (Phase 4 §F).

Raw ``httpx.AsyncClient`` with an injectable transport, mirroring the
ElevenLabs adapter. Every wire detail below is ⚠ UNVERIFIED memory (see
PHASE4_REPORT.md "Unverified register"): queue URL shape, status vocabulary,
result paths, error taxonomy, upload flow. Tests use ``httpx.MockTransport``.
"""

from studio.providers.fal.adapter import FalJobProvider, build_fal

__all__ = ["FalJobProvider", "build_fal"]
