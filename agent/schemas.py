"""Structured data passed between agent stages.

Every stage reads and writes these Pydantic models, so the LLM's output is
validated before the next stage uses it.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


Category = Literal["eligibility", "technical", "financial", "document", "timeline", "other"]
Decision = Literal["BID", "CONDITIONAL_BID", "NO_BID"]
CheckStatus = Literal["met", "partial", "gap", "unknown"]


class Requirement(BaseModel):
    id: str = Field(description="Short ID, e.g. R1, R2 ...")
    category: Category
    text: str = Field(description="The requirement in English, one sentence")
    mandatory: bool
    source: str = Field(description="Where it appears, e.g. 'Section 3.2' or 'Appendix B.1'")


class TenderExtraction(BaseModel):
    tender_id: str
    title: str
    issuer: str
    deadline: str = Field(description="Submission deadline as Gregorian YYYY-MM-DD, or '' if not stated")
    deadline_original: str = Field("", description="The deadline exactly as written in the document (may be Hijri)")
    estimated_value_sar: float | None = None
    summary: str = Field(description="2-3 sentence plain-English summary")
    requirements: list[Requirement]


class ChunkExtraction(BaseModel):
    """Extraction from one part of a long tender pack."""
    title: str = ""
    issuer: str = ""
    deadline: str = ""
    deadline_original: str = ""
    estimated_value_sar: float | None = None
    summary: str = ""
    requirements: list[Requirement]


class ListingScore(BaseModel):
    reference: str
    relevance: int = Field(ge=0, le=100)
    recommend: Literal["download", "maybe", "skip"]
    reason: str = Field(description="One short sentence")


class ListingScreen(BaseModel):
    items: list[ListingScore]


class RequirementCheck(BaseModel):
    requirement_id: str
    status: CheckStatus
    evidence: str = Field(description="Company file that proves it, e.g. 'certifications.json', or '' if none")
    note: str = Field(description="One short sentence explaining the status")


class Qualification(BaseModel):
    tender_id: str
    fit_score: int = Field(ge=0, le=100)
    decision: Decision
    reasons: list[str]
    blockers: list[str] = Field(description="Problems that disqualify or must be fixed before submission")
    checks: list[RequirementCheck]


class ProposalSection(BaseModel):
    title: str
    content: str


class ProposalDraft(BaseModel):
    tender_id: str
    revision: int = 1
    sections: list[ProposalSection]


class VerificationIssue(BaseModel):
    requirement_id: str
    problem: str
    fix: str


class VerificationReport(BaseModel):
    tender_id: str
    passed: bool
    issues: list[VerificationIssue]
