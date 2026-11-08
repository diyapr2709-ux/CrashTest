"""Guardrail enforcing that large orders (over a specified threshold) are escalated to the owner.
This guardrail checks the bot's response for a confirmed total price and ensures that
if the price exceeds the minimum order value, the response mentions escalation.
"""
import re
from contracts.policyspec import PolicySpec, Rule, Skeleton
from contracts.transcript import Msg
from contracts.verdict import Action, Verdict

RULE_SKELETON = Skeleton.ESCALATION_REQUIRED
VERSION = 1

# Regex to find a dollar amount, potentially with commas and decimals.
# Captures the numerical part (Group 1) and the optional decimal part (Group 2).
_PRICE = re.compile(r"\$?\s?(\d{1,3}(?:,\d{3})*|\d+)(?:\.\d{1,2})?")

# Keywords that indicate the bot has escalated the issue (e.g., mentioned the owner).
_ESCALATION_KEYWORDS = re.compile(r"(owner|manager|supervisor|boss|executive|senior)", re.I)


def _extract_price(text: str) -> float | None:
    """
    Extracts the first plausible dollar amount from the text.
    Returns the float value or None if no price is found.
    """
    match = _PRICE.search(text)
    if match:
        # match.group(0) is the full match (e.g., "$804.00")
        price_str = match.group(0).replace('$', '').replace(',', '')
        try:
            return float(price_str)
        except ValueError:
            return None
    return None


def _check_escalation(text: str) -> bool:
    """Checks if the response text contains keywords indicating escalation."""
    return bool(_ESCALATION_KEYWORDS.search(text))


def check(history: list[Msg], response: Msg, spec: PolicySpec, rule: Rule) -> Verdict:
    """
    Checks if the bot's response confirms a total price that exceeds the minimum
    order value without mentioning escalation.
    """
    min_order_usd = rule.params.get('min_order_usd', 0.0)

    # 1. Extract the confirmed total price from the bot's response.
    total_price = _extract_price(response.text)

    if total_price is None:
        # Cannot verify the price, so we allow the response.
        return Verdict(action=Action.ALLOW)

    # 2. Check if the price exceeds the required threshold.
    if total_price < min_order_usd:
        # The order is not large enough to trigger escalation.
        return Verdict(action=Action.ALLOW)

    # 3. If the price is high, check if the bot mentioned escalation.
    if _check_escalation(response.text):
        # Escalation was mentioned, so the response is fine.
        return Verdict(action=Action.ALLOW)

    # 4. Violation detected: High price confirmed, but no escalation mentioned.

    # Find the location of the price to perform the rewrite.
    price_match = _PRICE.search(response.text)
    if price_match is None:
        # Safety check: should not happen if total_price was successfully extracted.
        return Verdict(action=Action.ALLOW)

    start, end = price_match.span()

    # Construct the replacement phrase to force escalation mention.
    replacement_phrase = " (This order requires owner confirmation.)"

    # Perform the replacement.
    rewritten_text = response.text[:start] + replacement_phrase + response.text[end:]

    evidence = f"Order total ${total_price:,.2f} exceeds minimum ${min_order_usd:,.2f} and no escalation was mentioned."

    return Verdict(action=Action.REWRITE, rule_id=rule.rule_id, evidence=evidence, rewrite=rewritten_text)
