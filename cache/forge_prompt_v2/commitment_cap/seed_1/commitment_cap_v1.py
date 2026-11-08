"""
Guardrail for enforcing commitment caps (e.g., maximum refund or compensation amounts).
This guardrail checks if the bot's response offers a commitment (refund, comp, etc.)
that meets or exceeds the predefined monetary limit set by the rule.
"""
import re
from contracts.policyspec import PolicySpec, Rule, Skeleton
from contracts.transcript import Msg
from contracts.verdict import Action, Verdict

RULE_SKELETON = Skeleton.COMMITMENT_CAP
VERSION = 1

# Regex to find monetary values (e.g., $10, $10.50, $1,000).
# It captures the full amount string (group 0) and ensures it looks like a currency amount.
_MONEY_PATTERN = re.compile(r"\$?\s?(\d{1,3}(?:,\d{3})*|\d+)(\.\d{1,2})?")

# Keywords indicating a commitment
_COMMITMENT_KEYWORDS = ["refund", "comp", "complimentary", "discount", "credit", "waive"]

def _find_and_extract_amounts(text: str) -> list[tuple[int, int, float]]:
    """
    Finds all monetary values in the text and returns (start, end, value).
    """
    amounts = []
    for match in _MONEY_PATTERN.finditer(text):
        # Clean the matched string to get a float value
        amount_str = match.group(0).replace('$', '').replace(',', '')
        
        # Since the regex is constrained, conversion should succeed.
        value = float(amount_str)
        amounts.append((match.start(), match.end(), value))
    return amounts

def _check_commitment_violation(response: Msg, rule: Rule) -> tuple[bool, list[tuple[int, int, float]]]:
    """
    Checks if the response contains commitment keywords and if associated amounts
    meet or exceed the allowed cap defined in the rule.
    Returns (is_violating, list_of_violating_amounts).
    """
    text = response.text.lower()
    
    # 1. Check for commitment keywords
    is_commitment_mentioned = any(keyword in text for keyword in _COMMITMENT_KEYWORDS)
    
    if not is_commitment_mentioned:
        return False, []

    # 2. Get the cap limit
    try:
        max_usd = rule.params['max_usd']
        commitment_type = rule.params['type']
    except KeyError:
        # If parameters are missing, we cannot enforce the cap, so we allow it.
        return False, []

    # 3. Find all amounts
    amounts = _find_and_extract_amounts(response.text)
    
    violating_amounts = []
    
    # 4. Check if any amount meets or exceeds the cap (strict enforcement)
    for start, end, value in amounts:
        # We check if the value is greater than or equal to the cap.
        if value >= max_usd:
            violating_amounts.append((start, end, value))
            
    return bool(violating_amounts), violating_amounts


def check(history: list[Msg], response: Msg, spec: PolicySpec, rule: Rule) -> Verdict:
    """
    Guardrail check for commitment caps.
    """
    is_violating, violating_amounts = _check_commitment_violation(response, rule)

    if not is_violating:
        return Verdict(action=Action.ALLOW)

    # If violation occurs, we must rewrite the response to cap the amount.
    
    # We only need to fix the first violation found for a clean rewrite.
    start, end, offending_value = violating_amounts[0]
    
    # Determine the replacement string based on the cap
    cap_value = rule.params['max_usd']
    
    # Format the capped value nicely (e.g., $10.00)
    replacement_text = f"${cap_value:,.2f}"
    
    # Perform the rewrite
    fixed_text = response.text[:start] + replacement_text + response.text[end:]
    
    # Generate evidence
    evidence = f"Exceeded cap ({rule.params['max_usd']:.2f}). Found {offending_value:.2f} at {start}-{end}."
    
    # Ensure the rewrite actually changed the text
    if fixed_text == response.text:
        # If the rewrite failed to change the text, we block.
        return Verdict(action=Action.BLOCK, reason="Rewrite failed to change text.")

    return Verdict(
        action=Action.REWRITE,
        rule_id=rule.rule_id,
        evidence=evidence,
        rewrite=fixed_text
    )
