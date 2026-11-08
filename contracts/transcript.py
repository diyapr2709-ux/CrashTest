"""Conversations between a simulated customer and a tool. FROZEN contract.
Transcripts never carry labels. `sim_time` is the conversation's local wall-clock time."""
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Msg(BaseModel):
    """`at` is the message's local wall-clock time; hours/lead-time guardrails read it."""
    model_config = ConfigDict(extra="forbid")
    role: Literal["customer", "bot"]
    text: str
    turn: int = Field(ge=0)
    at: Optional[datetime] = None


class Transcript(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transcript_id: str
    business_id: str
    spec_version: int = Field(ge=1)
    tool_id: str
    persona_id: str
    attack_class: Optional[str] = None
    benign: bool
    seed: int
    sim_time: datetime
    attacker_model: str
    bot_model: str
    git_sha: str = "unknown"
    messages: List[Msg] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self):
        turns = [m.turn for m in self.messages]
        if turns != sorted(set(turns)):
            raise ValueError("turns must be strictly increasing")
        if self.benign == (self.attack_class is not None):
            raise ValueError("attack transcripts need attack_class; benign ones must not have one")
        return self
