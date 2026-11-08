"""Reference guardrail (hand-written). Forge-generated guardrails follow this shape.
Each price is tied to the CLOSEST item mention in the same sentence (ties go to the preceding one).
Prices next to refund/fee/total/discount words belong to other rules and are skipped. Quantities
are read from "2 cappuccinos" / "3 yards of mulch". If no quantity is stated and the price is a
whole multiple (2-50x) of the unit price, it is treated as an unstated quantity and allowed: we
prefer missing a rare misquote over rewriting a correct quote (false interventions cost trust).
Known gaps: prices written in words; one price shared by several items ("A and B are $3 each")."""
import re

from contracts.policyspec import PolicySpec, Rule, Skeleton
from contracts.transcript import Msg
from contracts.verdict import Action, Verdict

RULE_SKELETON = Skeleton.PRICE_MATCH
VERSION = 2

_PRICE = re.compile(r"\$\s?((?:\d{1,3}(?:,\d{3})+)|\d+)(\.\d{1,2})?")
_BOUNDARY = re.compile(r"[.!?]+(?=\s|$)|\n")
_CTX = (r"refund\w*|total|minimum|deposit|fees?|delivery|tips?|tax|surcharge|credit|"
        r"discount\w*|off|deal|back|charge|trip|after-hours|cancellation")
_CONTEXT = re.compile(r"\b(?:" + _CTX + r")\b", re.I)
_CONTEXT_TOKEN = re.compile(r"(?:" + _CTX + r")", re.I)
_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "seven": 7, "eight": 8, "nine": 9, "ten": 10, "dozen": 12}
_QTY = (r"(?<![$\d.,])\b(\d+|" + "|".join(sorted(_WORDS, key=len, reverse=True))
        + r")\s+(?:x\s+)?(?:(?:[a-z][a-z-]*\s+){1,2}of\s+)?")
_PATTERNS = {}


def _pattern(names):
    pat = _PATTERNS.get(names)
    if pat is None:
        alt = "|".join(re.escape(n) for n in names)
        pat = re.compile(r"(?:" + _QTY + r")?\b(" + alt + r")(?:e?s)?\b", re.I)
        _PATTERNS[names] = pat
    return pat


def _mentions(text, spec):
    by_name = {}
    for item in spec.catalog:
        by_name.setdefault(item.name.lower(), item)
    names = tuple(sorted(by_name, key=len, reverse=True))
    found = []
    for m in _pattern(names).finditer(text):
        q = m.group(1)
        qty = None if q is None else (int(q) if q.isdigit() else _WORDS[q.lower()])
        found.append((m.start(), m.end(), by_name[m.group(2).lower()], qty))
    return found


def _sentences(text):
    start = 0
    for b in _BOUNDARY.finditer(text):
        yield start, b.end()
        start = b.end()
    if start < len(text):
        yield start, len(text)


def _context_after(text, end, stop):
    words = re.findall(r"[\w-]+", text[end:stop])[:3]
    return any(_CONTEXT_TOKEN.fullmatch(w) for w in words)


def check(history, response: Msg, spec: PolicySpec, rule: Rule) -> Verdict:
    text = response.text
    mentions = _mentions(text, spec)
    fixes = []
    for s0, s1 in _sentences(text):
        local = [m for m in mentions if s0 <= m[0] < s1]
        for p in _PRICE.finditer(text, s0, s1):
            candidates = []
            before = [m for m in local if m[1] <= p.start()]
            after = [m for m in local if m[0] >= p.end()]
            if before:
                candidates.append((p.start() - before[-1][1], 0, before[-1],
                                   text[before[-1][1]:p.start()]))
            if after:
                candidates.append((after[0][0] - p.end(), 1, after[0],
                                   text[p.end():after[0][0]]))
            if not candidates:
                continue
            _, _, ref, gap = min(candidates, key=lambda c: (c[0], c[1]))
            if _CONTEXT.search(gap) or _context_after(text, p.end(), s1):
                continue
            quoted = float(p.group(1).replace(",", "") + (p.group(2) or ""))
            unit_price, qty = ref[2].price_usd, ref[3]
            if qty is None and unit_price > 0:
                ratio = quoted / unit_price
                if 2 <= round(ratio) <= 50 and abs(ratio - round(ratio)) < 1e-3:
                    continue
            expected = unit_price * (qty or 1)
            if abs(quoted - expected) > 0.005:
                fixes.append((p.start(), p.end(), quoted, expected, ref[2].name))
    if not fixes:
        return Verdict(action=Action.ALLOW)
    fixed = text
    for start, end, _, expected, _ in sorted(fixes, reverse=True):
        fixed = fixed[:start] + f"${expected:,.2f}" + fixed[end:]
    evidence = "; ".join(f"{n}: quoted ${q:,.2f}, catalog ${e:,.2f}" for _, _, q, e, n in fixes)
    return Verdict(action=Action.REWRITE, rule_id=rule.rule_id, evidence=evidence, rewrite=fixed)
