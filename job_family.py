"""Job family of a JD: which direction a resume should lead with.

Shared by tailoring (what to emphasise), assembly (headline, skills) and the
quality report. Families: security, ai, software, data, it, business; None = unrecognised."""
import re

# Job family -> headline. Every term is a fact already in the base resume or the
# profile (degree, listed skills); the headline names a direction, not a job held.
# The base headline is kept for AI/ML roles and when the title is not recognised.
HEADLINES = [
    ("security", re.compile(r"\bcyber|\bsecurity\b|\bsecops\b|\bsoc\b|\biam\b|\bgrc\b|\brisk\b", re.I),
     "Master of IT Graduate  ·  Python  ·  SQL  ·  Linux  ·  Requirements Analysis"),
    ("ai", re.compile(r"\bAI\b|artificial intelligence|machine learning|\bML\b|\bNLP\b|\bLLM|data scien|"
                      r"computer vision|deep learning", re.I), None),
    ("software", re.compile(r"\btest(?:ing)? analyst|\btester\b|\bQA\b", re.I), None),
    ("data", re.compile(r"\bdata\b|analytic|business intelligence|\bBI\b|power bi|reporting|"
                        r"insights?\b|database|\bDBA\b|databricks|warehouse|\betl\b", re.I),
     "Master of IT Graduate  ·  Data & Analytics  ·  SQL  ·  Power BI  ·  Python"),
    ("it", re.compile(r"support|service desk|help ?desk|technician", re.I), None),
    ("software", re.compile(r"software|developer|programmer|full[- ]stack|back[- ]?end|front[- ]?end|"
                            r"\bweb\b|devops", re.I),
     "Master of IT Graduate  ·  Software Development  ·  Python  ·  Flask  ·  SQL"),
    ("business", re.compile(r"business (?:systems|process|analyst)|systems owner|process specialist|"
                            r"\bproject (?:officer|coordinator|manager)\b", re.I),
     "Master of IT Graduate  ·  Business Intelligence  ·  Requirements Analysis  ·  SQL  ·  Power BI"),
    ("data", re.compile(r"\banalyst\b", re.I), None),
    ("it", re.compile(r"\bIT\b|\bICT\b|systems?|infrastructure|cloud|network|administrator|engineer|"
                      r"m365|microsoft|workplace|telecom", re.I),
     "Master of IT Graduate  ·  Microsoft 365  ·  Windows  ·  Python  ·  SQL"),
]
def headline_for(family):
    """The headline text for a family (entries without text share the family's main one)."""
    return next((h for f, _, h in HEADLINES if f == family and h), None)


def job_title(jd_text):
    match = re.search(r"^Title:\s*(.+)$", jd_text or "", re.M)
    if match:
        return match.group(1).strip()
    return next((line.strip() for line in (jd_text or "").splitlines() if line.strip()), "")


def job_family(title):
    return next((family for family, pattern, _ in HEADLINES if pattern.search(title or "")), None)
