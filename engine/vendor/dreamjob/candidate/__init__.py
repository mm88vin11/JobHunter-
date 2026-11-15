"""Fact-grounded candidate profile adaptation."""

from .data import CandidateData, CandidateKnowledgeBase, Fact, MasterProfile, load_candidate_data
from .service import CandidateAdapter, CandidateVariant, UnsafeVariantError

__all__ = [
    "CandidateAdapter",
    "CandidateData",
    "CandidateKnowledgeBase",
    "CandidateVariant",
    "Fact",
    "MasterProfile",
    "UnsafeVariantError",
    "load_candidate_data",
]
