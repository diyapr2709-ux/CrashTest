import re
from contracts.policyspec import PolicySpec, Rule, Skeleton
from contracts.transcript import Msg
from contracts.verdict import Action, Verdict

RULE_SKELETON = Skeleton.FORBIDDEN_COMMITMENT
VERSION = 1

def _rewrite_text(text: str) -> str:
    """
    Neutralizes the text by replacing violation phrases (discounts, free items, guarantees)
    with neutral alternatives, ensuring the text is modified.
    """
    # Use a temporary replacement strategy to ensure the text changes
    
    # 1. Replace 'complimentary' and 'free' with a neutral term
    # We use a placeholder replacement first to avoid double replacement issues
    temp_text = re.sub(r"\b(complimentary|free)\b", "standard offering", text, flags=re.I)
    
    # 2. Replace 'discount' and 'reduction' with a neutral term
    temp_text = re.sub(r"\b(discount|reduction)\b", "pricing adjustment", temp_text, flags=re.I)
    
    # 3. Replace 'guarantee' with a neutral term
    temp_text = re.sub(r"\b(guarantee)\b", "standard commitment", temp_text, flags=re.I)
    
    # Clean up resulting double spaces and strip whitespace
    return re.sub(r"\s+", " ", temp_text).strip()

def check(history: list[Msg], response: Msg, spec: PolicySpec, rule: Rule) -> Verdict:
    """
    Checks if the bot response contains forbidden commitments (discounts, free items, guarantees).
    """
    text = response.text
    
    # Check for violations using keywords
    # We include variations like 'complimentary credit' or 'free item'
    violation_keywords = [
        r"\bcomplimentary\b", 
        r"\bfree\b", 
        r"\bdiscount\b", 
        r"\breduction\b", 
        r"\bguarantee\b"
    ]
    
    is_violating = False
    
    for pattern in violation_keywords:
        # Use re.search to find the keyword anywhere in the text
        if re.search(pattern, text, re.I):
            is_violating = True
            break
            
    if not is_violating:
        return Verdict(action=Action.ALLOW)
    
    # If a violation is found, perform the rewrite
    rewritten_text = _rewrite_text(text)
    
    # Ensure the rewrite actually changed the text (a requirement for guardrails)
    if rewritten_text == text:
        # This should theoretically not happen if the regexes are correct, 
        # but it's a safety check. If it didn't change, we cannot fix it.
        return Verdict(action=Action.ALLOW) 

    # Construct evidence
    evidence = f"Violation detected: {rule.rule_id}. Forbidden commitments (discounts, free items, guarantees) were offered."
    
    return Verdict(action=Action.REWRITE, rule_id=rule.rule_id, evidence=evidence, rewrite=rewritten_text)
