"""PolicySpec: the business's rules as one typed object. FROZEN contract.
Hours are local to `timezone`; close earlier than open runs past midnight; unlisted days are closed.
`failure_corpus` may only hold DEV failures (never holdout)."""
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Type
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts.verdict import Severity, Violation

SCHEMA_VERSION = "1.1"
Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Vertical(str, Enum):
    RESTAURANT = "restaurant"
    CAFE = "cafe"
    HVAC = "hvac"
    LANDSCAPING = "landscaping"


class Channel(str, Enum):
    WEB_CHAT = "web_chat"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    VOICE = "voice"
    REVIEW_REPLY = "review_reply"


class Skeleton(str, Enum):
    PRICE_MATCH = "price_match"
    HOURS_BOUND = "hours_bound"
    SERVICE_AREA_BOUND = "service_area_bound"
    LEAD_TIME_BOUND = "lead_time_bound"
    COMMITMENT_CAP = "commitment_cap"
    FORBIDDEN_COMMITMENT = "forbidden_commitment"
    WARRANTY_CLAIM = "warranty_claim"
    ALLERGEN_CLAIM = "allergen_claim"
    AVAILABILITY_CLAIM = "availability_claim"
    ROLE_SCOPE = "role_scope"
    PII_LEAK = "pii_leak"
    ESCALATION_REQUIRED = "escalation_required"


# ---- typed params per skeleton (stored as a dict on Rule, validated here)
class NoParams(_Strict):
    pass


class CommitmentCapParams(_Strict):
    type: str
    max_usd: float = Field(ge=0)


class ForbiddenCommitmentParams(_Strict):
    types: List[str] = Field(min_length=1)


class LeadTimeParams(_Strict):
    service: str
    min_days: int = Field(ge=0)
    business_days: bool = False


class EscalationParams(_Strict):
    min_order_usd: float = Field(ge=0)


SKELETON_PARAMS: Dict[Skeleton, Type[BaseModel]] = {s: NoParams for s in Skeleton}
SKELETON_PARAMS.update({
    Skeleton.COMMITMENT_CAP: CommitmentCapParams,
    Skeleton.FORBIDDEN_COMMITMENT: ForbiddenCommitmentParams,
    Skeleton.LEAD_TIME_BOUND: LeadTimeParams,
    Skeleton.ESCALATION_REQUIRED: EscalationParams,
})


class Hours(_Strict):
    day: Day
    open: str = Field(pattern=HHMM)
    close: str = Field(pattern=HHMM)


class CatalogItem(_Strict):
    item_id: str
    name: str
    price_usd: float = Field(ge=0)
    unit: str = "each"
    allergens: List[str] = Field(default_factory=list)
    available: bool = True


class Warranty(_Strict):
    warranty_id: str
    scope: Literal["parts", "labor", "plant", "workmanship"]
    months: int = Field(ge=0)
    item_ids: List[str] = Field(default_factory=list)
    conditions: str = ""


class Rule(_Strict):
    rule_id: str
    skeleton: Skeleton
    description: str
    severity: Severity
    params: Dict[str, Any] = Field(default_factory=dict)
    fine_usd: Optional[float] = Field(default=None, ge=0)
    enforcement_prob: Optional[float] = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _params_typed(self):
        SKELETON_PARAMS[self.skeleton].model_validate(self.params)
        if self.severity == "legal" and (self.fine_usd is None or self.enforcement_prob is None):
            raise ValueError(f"legal rule {self.rule_id} needs fine_usd and enforcement_prob")
        return self

    def typed_params(self):
        return SKELETON_PARAMS[self.skeleton].model_validate(self.params)


class Economics(_Strict):
    avg_ticket_usd: float = Field(ge=0)
    margin: float = Field(ge=0, le=1)
    labor_rate_usd_hr: float = Field(ge=0)
    refund_cost_usd: float = Field(ge=0)
    chargeback_fee_usd: float = Field(ge=0)
    monthly_conversations: int = Field(ge=0)
    conversion_rate: float = Field(ge=0, le=1)
    redemption_rate: float = Field(ge=0, le=1)
    trip_hours: Optional[float] = Field(default=None, ge=0)


class ExistingTool(_Strict):
    tool_id: str
    name: str
    channel: Channel
    log_path: Optional[str] = None


class PolicySpec(_Strict):
    schema_version: Literal["1.1"] = SCHEMA_VERSION
    business_id: str
    vertical: Vertical
    timezone: str
    channels: List[Channel]
    hours: List[Hours]
    service_area_zips: Optional[List[str]] = None
    after_hours_surcharge_usd: Optional[float] = Field(default=None, ge=0)
    catalog: List[CatalogItem]
    warranties: List[Warranty] = Field(default_factory=list)
    rules: List[Rule]
    economics: Economics
    existing_tools: List[ExistingTool] = Field(default_factory=list)
    failure_corpus: List[Violation] = Field(default_factory=list)
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _consistent(self):
        try:
            ZoneInfo(self.timezone)
        except Exception:
            raise ValueError(f"unknown timezone {self.timezone!r}") from None
        for name, ids in (("rule_id", [r.rule_id for r in self.rules]),
                          ("item_id", [c.item_id for c in self.catalog]),
                          ("warranty_id", [w.warranty_id for w in self.warranties]),
                          ("tool_id", [t.tool_id for t in self.existing_tools])):
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {name}")
        items = {c.item_id for c in self.catalog}
        for w in self.warranties:
            if set(w.item_ids) - items:
                raise ValueError(f"warranty {w.warranty_id} cites unknown items")
        skeletons = {r.skeleton for r in self.rules}
        if Skeleton.SERVICE_AREA_BOUND in skeletons and not self.service_area_zips:
            raise ValueError("service_area_bound rule needs service_area_zips")
        if Skeleton.WARRANTY_CLAIM in skeletons and not self.warranties:
            raise ValueError("warranty_claim rule needs warranties")
        unknown = sorted({v.rule_id for v in self.failure_corpus} - {r.rule_id for r in self.rules})
        if unknown:
            raise ValueError(f"failure_corpus cites unknown rules: {unknown}")
        return self

    def rule(self, rule_id: str) -> Rule:
        for r in self.rules:
            if r.rule_id == rule_id:
                return r
        raise KeyError(rule_id)
