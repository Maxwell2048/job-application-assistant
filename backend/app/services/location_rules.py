"""Location checks use employer prose, never collector annotations."""
import re


def employer_body(text):
    text = text or ""
    marker = "[Employer job description]"
    if text.lstrip().startswith("[SEEK listing metadata"):
        return text.split(marker, 1)[1].strip() if marker in text else ""
    return text.strip()


def remote_evidence(text, listing_location=""):
    body = employer_body(text)
    clauses = [s.strip() for s in re.split(r"[\n;]|(?<=[.!?])\s+", body) if s.strip()]
    city = r"(?:Sydney|Melbourne|Brisbane|Canberra|Adelaide|Wollongong|Gold Coast|Hobart|Darwin|NSW|VIC|QLD|ACT|Tasmania)"
    restrictions = [
        r"\b(?:must|required|only open|need to|candidates? currently).{0,100}\b(?:based|live|resid|located).{0,35}" + city,
        r"\b(?:this role|the role|role|position|opportunity|candidates?|applicants?).{0,70}\b(?:based|located).{0,40}" + city,
        r"(?:^based in\s+|\bremotely from\s+)" + city,
        r"\b(?:[1-7]|one|two|three|four|five)\s+days?.{0,55}\b(?:office|CBD|on[ -]site)\b",
        r"\b(?:office|CBD|on[ -]site)\b.{0,55}\b(?:[1-7]|one|two|three|four|five)\s+days?",
        r"\b(?:remote|work from home|WFH)\b.{0,30}\b(?:not available|not offered|not permitted)\b",
        r"\b(?:no|not a)\s+(?:fully\s+)?remote\b",
        r"\b(?:up to|maximum).{0,15}\b\d+\s+days?.{0,35}\b(?:year|annually)\b",
    ]
    for clause in clauses:
        if any(re.search(pattern, clause, re.I) for pattern in restrictions):
            return {"status": "restricted", "quote": clause, "reason": "雇主要求外州本地居住、出勤或限制异地办公"}
    remote = r"(?:\b(?:fully|100\s*%)\s*remote\b|\bremote[- ]first\b|\bwork(?:ing)?\s+from\s+(?:home|anywhere)\b|\bremote\s+(?:role|position|work(?:ing)?)\b)"
    nationwide = r"(?:anywhere\s+(?:in|across)\s+Australia|across\s+Australia|Australia[- ]wide|anywhere\s+in\s+WA|from\s+Perth)"
    candidates = clauses + [a + " " + b for a, b in zip(clauses, clauses[1:]) if len(a) + len(b) < 400]
    for clause in candidates:
        if re.search(remote, clause, re.I) and re.search(nationwide, clause, re.I):
            return {"status": "confirmed", "quote": clause, "reason": "雇主明确允许澳洲任意地点或珀斯远程办公"}
        if re.search(remote, clause, re.I) and listing_location.casefold() in ("all australia", "australia", "australia-wide"):
            return {"status": "confirmed", "quote": clause, "reason": "全国地点广告明确提供全远程工作"}
    mention = next((s for s in clauses if re.search(r"\bremote\b|hybrid|work from home|\bWFH\b", s, re.I)), "")
    return {"status": "unconfirmed", "quote": mention, "reason": "雇主未明确允许在珀斯长期远程办公"}
