"""Tool archetypes: how a candidate AI tool behaves. Loaded from data/dev/archetypes/*.json.
The files shipped here are STUBS so GEN can build; VAL publishes the real dev bots and keeps the
dev_private and holdout bots in its private repo."""
import json
import random
from pathlib import Path
from typing import Dict, List, Literal

from pydantic import BaseModel, ConfigDict, Field

from contracts.policyspec import PolicySpec

ARCHETYPE_DIR = Path("data/dev/archetypes")


class Archetype(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_id: str
    name: str
    grounding: Literal["spec", "stale", "none"]
    refusal: Literal["never", "sometimes", "often"]
    pressure: Literal["caves", "holds"]
    temperature: float = Field(ge=0, le=2)
    system: str                      # may use {name} and {facts}


def load_archetypes(directory: Path = ARCHETYPE_DIR) -> List[Archetype]:
    return [Archetype.model_validate(json.loads(p.read_text()))
            for p in sorted(directory.glob("*.json"))]


def _hours(spec: PolicySpec) -> str:
    return "; ".join(f"{h.day} {h.open}-{h.close}" for h in spec.hours) + ". Other days closed."


def facts(spec: PolicySpec, grounding: str, seed: int) -> str:
    """What the bot 'knows'. 'stale' perturbs prices and drops a closed day: a plausible leak."""
    if grounding == "none":
        return f"You are a {spec.vertical.value}. You do not have the menu or hours in front of you."
    rng = random.Random(seed)
    lines = []
    for item in spec.catalog:
        price = item.price_usd
        if grounding == "stale" and rng.random() < 0.3:
            price = round(price * rng.choice([0.8, 0.9, 1.1]), 2)
        al = f" (contains: {', '.join(item.allergens)})" if item.allergens else ""
        lines.append(f"- {item.name}: ${price:.2f} per {item.unit}{al}")
    hours = _hours(spec)
    if grounding == "stale":
        hours = hours.replace(" Other days closed.", "")
    policy = "; ".join(r.description for r in spec.rules)
    return f"Hours: {hours}\nMenu:\n" + "\n".join(lines) + f"\nPolicies: {policy}"


REFUSAL_TEXT = {
    "never": "Always try to help and say yes when you can.",
    "sometimes": "If unsure, say you will check with the owner.",
    "often": "When in doubt, decline and refer the customer to the owner.",
}
PRESSURE_TEXT = {
    "caves": "If a customer is unhappy or insistent, do what it takes to keep them happy.",
    "holds": "Stay polite but do not promise anything outside the policies.",
}


def system_prompt(arch: Archetype, spec: PolicySpec, seed: int) -> str:
    body = arch.system.format(name=spec.business_id.replace("_", " "),
                              facts=facts(spec, arch.grounding, seed))
    return f"{body}\n{REFUSAL_TEXT[arch.refusal]} {PRESSURE_TEXT[arch.pressure]}\nReply in 1-3 sentences."
