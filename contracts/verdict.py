"""Violations (VAL-owned labels) and Verdicts (guardrail decisions). FROZEN contract.
Dollars are computed only by VAL's pricer, never by a guardrail."""
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

Severity = Literal["money", "safety", "legal"]
LabelSource = Literal["judge", "human", "planted"]


class Violation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str
    transcript_id: str
    turn: int = Field(ge=0)
    span: str
    severity: Severity
    source: LabelSource
    exposure_usd: Optional[float] = Field(default=None, ge=0)


class Action(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    REWRITE = "rewrite"


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action
    rule_id: Optional[str] = None
    evidence: str = ""
    rewrite: Optional[str] = None

    @model_validator(mode="after")
    def _consistent(self):
        if self.action != Action.ALLOW and not self.rule_id:
            raise ValueError("block/rewrite verdicts must cite a rule_id")
        if self.action == Action.REWRITE and not self.rewrite:
            raise ValueError("rewrite verdicts must carry the corrected reply")
        if self.action != Action.REWRITE and self.rewrite is not None:
            raise ValueError("only rewrite verdicts carry a rewrite")
        return self
