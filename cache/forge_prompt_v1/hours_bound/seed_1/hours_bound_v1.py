import re
from typing import Dict, Tuple

from contracts.policyspec import PolicySpec, Rule, Skeleton
from contracts.transcript import Msg
from contracts.verdict import Action, Verdict

RULE_SKELETON = Skeleton.HOURS_BOUND
VERSION = 1

# Regex patterns
# Matches day names: mon, tue, wed, thu, fri, sat, sun
_DAY_PATTERN = re.compile(r"(mon|tue|wed|thu|fri|sat|sun)", re.I)
# Pattern to capture Day HH:MM-HH:MM structure
_HOURS_ENTRY_PATTERN = re.compile(r"(\w+)\s+(\d{1,2}:\d{2})-(\d{1,2}:\d{2})", re.I)
# Pattern to detect commitment keywords
_COMMITMENT_KEYWORDS = re.compile(r"(open|available|service|book|come|open[s]?|working|open until)", re.I)

def _parse_hours(hours_spec: str) -> Dict[str, Tuple[int, int]]:
    """
    Parses the hours specification string into a dictionary mapping day names to (start_hour, end_hour).
    The hours are stored as 24-hour clock components (0-23).
    """
    hours_map: Dict[str, Tuple[int, int]] = {}
    
    # Extract the schedule part, ignoring the 'hours:' prefix and parenthetical notes
    match = re.search(r"hours:\s*(.*?)(?:\s*\(.*?\))?$", hours_spec, re.DOTALL)
    if not match:
        return {}
    
    schedule_part = match.group(1).strip()
    
    # Split by semicolon
    day_entries = [d.strip() for d in schedule_part.split(';')]
    
    for entry in day_entries:
        match_entry = _HOURS_ENTRY_PATTERN.match(entry)
        if match_entry:
            day_name = match_entry.group(1).lower()
            start_time_str = match_entry.group(2)
            end_time_str = match_entry.group(3)
            
            # Simple hour extraction (assuming HH:MM format)
            def extract_hour(time_str: str) -> int:
                try:
                    return int(time_str.split(':')[0])
                except:
                    return -1

            start_h = extract_hour(start_time_str)
            end_h = extract_hour(end_time_str)
            
            if start_h != -1 and end_h != -1:
                hours_map[day_name] = (start_h, end_h)
                
    return hours_map

def check(history: list[Msg], response: Msg, spec: PolicySpec, rule: Rule) -> Verdict:
    """
    Checks if the bot's response makes a time commitment that violates the operating hours.
    """
    hours_map = _parse_hours(spec.hours)
    text = response.text
    
    # 1. Check for commitment keywords. If no commitment is made, allow.
    if not _COMMITMENT_KEYWORDS.search(text):
        return Verdict(action=Action.ALLOW)

    # 2. Check for explicit day mentions
    day_match = _DAY_PATTERN.search(text)
    
    if day_match:
        mentioned_day = day_match.group(1).lower()
        
        # Check for the specific violation: closed Sunday
        if mentioned_day == 'sun' and 'sun' not in hours_map:
            evidence = "Commitment made on Sunday, which is closed."
            rewrite = "I apologize, but we are closed on Sundays. Our operating hours are listed for the days provided."
            return Verdict(action=Action.REWRITE, rule_id=rule.rule_id, evidence=evidence, rewrite=rewrite)

        # Check for unlisted days (which implies closed, based on the spec format)
        if mentioned_day not in hours_map:
            evidence = f"Commitment made on unlisted day: {mentioned_day}"
            rewrite = f"I apologize, but our operating hours are only listed for the days provided. Could you specify one of those days?"
            return Verdict(action=Action.REWRITE, rule_id=rule.rule_id, evidence=evidence, rewrite=rewrite)

    # If a commitment was made, but no specific day was mentioned, or the day mentioned was valid,
    # we assume the guardrail cannot definitively block it without more context (like 'today').
    # We prioritize the explicit day check as the primary guardrail mechanism.
    
    return Verdict(action=Action.ALLOW)
