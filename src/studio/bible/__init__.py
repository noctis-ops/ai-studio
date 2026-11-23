"""Bible proposals (Phase 5 §L / S14).

`studio.bible` owns the *proposal* path around the Series Bible: the strict
validator, the pure apply function, and the service that proposes, accepts and
rejects. The bible itself (`series_bible`) still has exactly one write path,
`BibleRepo.create_version`, and nothing here applies a proposal automatically.
"""

from __future__ import annotations

from .apply import apply_proposal
from .proposal import (
    PROMPT_NAME,
    SCHEMA_NAME,
    STAGE,
    ProposalInputs,
    accept_proposal,
    gather_inputs,
    propose,
    reject_proposal,
)
from .schema import BibleUpdate, proposal_to_json, validate_proposal

__all__ = [
    "PROMPT_NAME",
    "SCHEMA_NAME",
    "STAGE",
    "BibleUpdate",
    "ProposalInputs",
    "accept_proposal",
    "apply_proposal",
    "gather_inputs",
    "proposal_to_json",
    "propose",
    "reject_proposal",
    "validate_proposal",
]
