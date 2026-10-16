"""Conservative listing rules shared by search and screening."""
import re

LEVEL = re.compile(r"\b(?:senior|sr\.?|lead|principal|head|director|architect|mid[- ]?level|intermediate)\b", re.I)
JUNIOR = re.compile(r"\b(?:junior|graduate|trainee|entry[- ]level|assistant|associate)\b", re.I)

def title_parts(title):
    return [p.strip() for p in re.split(r"\s*[|/]\s*", title or "") if p.strip()]

def ambiguous_level(title):
    parts = title_parts(title)
    return bool(JUNIOR.search(title or "") or (len(parts) > 1 and any(not LEVEL.search(p) for p in parts)))

def excluded_title(title, pattern):
    # Only clearly advanced titles are dropped before JD retrieval.
    if not pattern or ambiguous_level(title):
        return False
    parts = title_parts(title)
    return bool(parts and all(re.match(r"(?:senior|sr\.?|lead|principal|head|director|mid[- ]?level|intermediate)\b", p, re.I)
                              and re.search(pattern, p, re.I) for p in parts))

def fifo_required(text):
    for clause in re.split(r"[;\n]|(?<=[.!?])\s+", text or ""):
        if not re.search(r"\b(?:FIFO|DIDO|BIBO|fly[- ]in[- ]fly[- ]out)\b", clause, re.I):
            continue
        # A mention alone (clients, colleagues, or a negation) is not an applicant requirement.
        if re.search(r"\b(?:no|not|without|never)\b.{0,30}\b(?:FIFO|DIDO|BIBO|fly[- ]in[- ]fly[- ]out)\b|\b(?:FIFO|DIDO|BIBO)\b.{0,25}\b(?:not required|not necessary|optional)\b", clause, re.I):
            continue
        if re.search(r"\b(?:FIFO|DIDO|BIBO)\b.{0,25}\b(?:role|position|roster|required|mandatory)\b|\b(?:must|required|roster|position|role)\b.{0,35}\b(?:FIFO|DIDO|BIBO|fly[- ]in[- ]fly[- ]out)\b", clause, re.I):
            return True
    return False

PERTH_SUBURBS = re.compile(r"\b(?:Canning Vale|Cannington|Bentley|Welshpool|Victoria Park|Fremantle|Joondalup|Osborne Park|West Perth|East Perth|Subiaco|Nedlands|Claremont|Malaga|Balcatta|Belmont|Kewdale|Midland|Gosnells|Armadale|Cockburn|Rockingham|Baldivis|Wangara|Wanneroo|Ellenbrook|Bayswater|Morley|South Perth|Burswood|Applecross|Bibra Lake|Henderson|Jandakot|Scarborough|Innaloo|Leederville|Mount Lawley|Kalamunda|Maddington)\b", re.I)

def perth_location(location):
    return bool(re.search(r"\bPerth\b", location or "", re.I) or PERTH_SUBURBS.search(location or ""))


TECHNICAL_TITLE = re.compile(r"\b(?:software|developer|programmer|data|digital|ICT|IT|cyber|network|systems?|analyst|automation|computing|computer|AI|machine learning|application|web|DevOps|cloud)\b", re.I)
NON_IT_PROFESSION = re.compile(r"\b(?:painters?|carpenters?|plumbers?|electricians?|labourers?|factory hand|accessory fitters?|chefs?|cooks?|baristas?|waiters?|nurses?|nursing|dentists?|dental|sonographers?|physiotherapists?|occupational therapists?|speech (?:therapists?|pathologists?)|psychologists?|geologists?|mine surveyors?|mine surveying|civil engineers?|mining engineers?|law graduates?|teachers?|education assistants?|receptionists?)\b", re.I)

def clearly_non_it_title(title):
    return bool(NON_IT_PROFESSION.search(title or "") and not TECHNICAL_TITLE.search(title or ""))


IT_TITLE = re.compile(r"\b(?:IT|ICT|software|developers?|cyber\w*|DevOps|helpdesk|service desk|desktop support|application support|data (?:analysts?|engineers?|scientists?)|business intelligence|machine learning|AI engineer|web (?:developers?|designers?)|database|cloud (?:engineers?|support|administrators?)|systems? (?:administrators?|engineers?)|network (?:engineers?|administrators?)|test analysts?|QA engineers?|UX|UI)\b", re.I)
IT_CONTEXT = re.compile(r"\b(?:software development|software engineering|software testing|software implementation|Python|SQL|JavaScript|React|IT support|information technology|ICT|ITIL|ServiceNow|Active Directory|network administration|cybersecurity|machine learning|Power BI|data warehouse|database|SaaS|API integration|systems? implementation|software testing)\b", re.I)

def it_evidence(title, context="", subclassification=""):
    if IT_TITLE.search(title or ""):
        return True
    if subclassification.casefold() in {"developers/programmers", "engineering - software", "help desk & it support", "business/systems analysts", "testing & quality assurance", "networks & systems administration", "database development & administration", "security", "web development & production"}:
        return True
    return bool(IT_CONTEXT.search(context or ""))
