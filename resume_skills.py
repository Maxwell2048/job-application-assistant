"""Candidate-confirmed skill presentation preferences (2026-10-23).

Apply only to skill lists. Project descriptions and their verified model names
are unchanged. MCP/harness are user-declared skills, not inferred project work.
"""
import copy
import re

AI_ENGINEERING = "AI Engineering: Harness Engineering · MCP (Model Context Protocol)"
MODEL_ADAPTATION = "Model Fine-tuning: LoRA · Transfer Learning"
LLM_TOOLING = "LLM Tooling: Hugging Face Transformers · Ollama · llama.cpp · OpenAI-compatible APIs"
PREFERENCES = [(AI_ENGINEERING, ("harness", "mcp")),
               (MODEL_ADAPTATION, ("lora", "fine-tun")),
               (LLM_TOOLING, ("hugging face", "ollama"))]
# Confirmed 2026-11-25 and cited from the profile (build_resume.frameworks_line), not
# from the base resume, so it is grouped with the declared rows but never added on re-export.
LLM_FRAMEWORKS = "LLM Frameworks: LangChain · LangGraph"
DECLARED_GROUP = PREFERENCES + [(LLM_FRAMEWORKS, ("langchain", "langgraph"))]

YOLO = re.compile(r"\bYOLO(?:v?\d+[a-z]*)?(?:\s*/\s*YOLO(?:v?\d+[a-z]*)?)*\b", re.I)


def clean_skill_text(text):
    text = YOLO.sub("", text)
    text = re.sub(r"([,;·])\s*([,;·])", r"\2", text)
    text = re.sub(r"(:)\s*[,;·]\s*", r"\1 ", text)
    text = re.sub(r"[,;·]\s*$", "", text)
    return text.strip()


def skill_priority(line):
    text = line["text"].lower()
    return next((i for i, (_, markers) in enumerate(DECLARED_GROUP)
                 if all(marker in text for marker in markers)), len(DECLARED_GROUP))


def has_ai_engineering(texts):
    text = " ".join(texts)
    return bool(re.search(r"\bharness\b", text, re.I) and re.search(r"\bMCP\b", text))


def split_categories(lines):
    """One skill category per line: '◆ A  x · y     ◆ B  z' becomes two lines
    with the same citation, so categories can be ordered individually."""
    out = []
    for line in lines:
        pieces = [p.strip() for p in re.split(r"◆\s*", line.get("text", "")) if p.strip()]
        # 'AI / ML  PyTorch · ...' (two-space label, as in the base table) -> 'AI / ML: PyTorch · ...'
        pieces = [re.sub(r"^([^:·]{2,28}?)\s{2,}(?=\S)", r"\1: ", p) for p in pieces]
        # One separator style across rows: 'Label: a, b' -> 'Label: a · b'.
        pieces = [re.sub(r"^([^:]{2,28}:\s*)(.*)$", lambda m: m.group(1) + re.sub(r"\s*,\s*", " · ", m.group(2)), p)
                  for p in pieces]
        if len(pieces) <= 1:
            out.append({**line, "text": pieces[0] if pieces else line.get("text", "")})
            continue
        out += [{**line, "text": piece, "basis": [dict(b) for b in line.get("basis", [])]} for piece in pieces]
    return out


_ITEM_SPLIT = re.compile(r"\s*(?:·|,|;|/|\s{2,}|:)\s*")
# Fragments of compound skill names that are ordinary English words in a JD
# ('Self/Cross-Attention' must not match 'self-service').
_GENERIC = {"self", "cross-attention", "attention", "text classification", "transfer learning",
            "encoder-decoder", "transformer", "apis", "css", "nli"}


def jd_relevance(text, jd_text):
    """How many of a skill line's items the JD names (whole-word, case-insensitive)."""
    jd = (jd_text or "").casefold()
    body = text.split(":", 1)[1] if ":" in text[:30] else text  # the category label is not a skill
    items = {item.strip().casefold() for item in _ITEM_SPLIT.split(body)
             if len(item.strip()) >= 2 and item.strip().casefold() not in _GENERIC}
    return sum(1 for item in items
               if re.search(r"(?<![a-z0-9])" + re.escape(item) + r"(?![a-z0-9])", jd))


RESEARCH_LABELS = {"ai / ml", "deep learning", "nlp"}


def order_by_jd(lines, jd_text, declared_first=False, research_last=False):
    """Most JD-relevant categories first. The confirmed AI tooling lines stay
    together as one group (shown alongside each other, as the user asked) and
    move by their best line; ties keep the base resume's order, where the
    declared group comes last."""
    declared = [l for l in lines if skill_priority(l) < len(DECLARED_GROUP)]
    declared.sort(key=skill_priority)
    others = [l for l in lines if skill_priority(l) == len(DECLARED_GROUP)]
    units = [(jd_relevance(l["text"], jd_text), i, [l]) for i, l in enumerate(others)]
    if declared:
        best = max(jd_relevance(l["text"], jd_text) for l in declared)
        units.append((float("inf") if declared_first else best, len(others), declared))
    if research_last:
        # Outside AI roles, equally relevant ML research rows follow the general ones.
        def demoted(group):
            if skill_priority(group[0]) < len(DECLARED_GROUP):
                return 200  # the declared AI tooling group goes last on a tie
            return 100 if group[0]["text"].split(":", 1)[0].strip().casefold() in RESEARCH_LABELS else 0
        units = [(score, pos + demoted(group), group) for score, pos, group in units]
    units.sort(key=lambda u: (-u[0], u[1]))
    return [line for _, _, group in units for line in group]


def dedupe_items(lines):
    """An item already listed in an earlier row ('Transfer Learning' under both
    Model Fine-tuning and Deep Learning) is dropped from the later row; a row
    is never emptied."""
    seen = set()
    for line in lines:
        label, sep, body = line["text"].partition(":")
        if not sep:
            continue
        items = [i.strip() for i in body.split("·") if i.strip()]
        keep = [i for i in items if i.casefold() not in seen] or items
        seen.update(i.casefold() for i in items)
        if len(keep) < len(items):
            line["text"] = f"{label}: " + " · ".join(keep)
    return lines


def apply_to_sections(sections, resume_entry, jd_text=None, declared_first=False, extra_lines=(),
                      research_last=False):
    """Keep source citations while changing display and adding a declared fact.
    With jd_text, skill categories are ordered by relevance to the job;
    declared_first keeps the confirmed AI tooling group on top (AI roles).
    extra_lines are already-cited skill rows to add when not yet present."""
    target = None
    for section in sections:
        if "SKILL" not in section["title"].upper() or "LANGUAGE" in section["title"].upper():
            continue
        target = target or section
        section["lines"] = split_categories(section["lines"])
        for line in section["lines"]:
            line["text"] = clean_skill_text(line["text"])
    if target is not None:
        for text, markers in PREFERENCES:
            existing = " ".join(line["text"] for line in target["lines"]).lower()
            if all(marker in existing for marker in markers):
                continue
            raw = next((line.strip() for line in resume_entry["text"].splitlines()
                        if all(marker in line.lower() for marker in markers)), None)
            if raw:
                target["lines"].append({"kind": "text", "text": text,
                    "basis": [{"material_id": "RESUME", "quote": raw,
                               **{key: resume_entry[key] for key in ("kind", "source", "section", "line_start", "line_end")}}]})
        for extra in extra_lines:
            label = extra["text"].split(":", 1)[0].casefold()
            if not any(line["text"].split(":", 1)[0].casefold() == label for line in target["lines"]):
                target["lines"].append(extra)
        if jd_text:
            target["lines"] = dedupe_items(order_by_jd(target["lines"], jd_text, declared_first, research_last))
        else:
            target["lines"].sort(key=skill_priority)
    return sections


def apply_to_export(parsed):
    """Honor preferences on re-export of older resumes as well as new ones."""
    parsed = copy.deepcopy(parsed)
    target = None
    texts = []
    for section in parsed["sections"]:
        if "SKILL" not in section["title"].upper() or "LANGUAGE" in section["title"].upper():
            continue
        target = target or section
        for block in section["blocks"]:
            for line in block["lines"]:
                line["text"] = clean_skill_text(line["text"])
                texts.append(line["text"])
    if target is not None:
        for text, markers in PREFERENCES:
            if not all(marker in " ".join(texts).lower() for marker in markers):
                target["blocks"].append({"heading": "", "lines": [{"kind": "text", "text": text}]})
                texts.append(text)
        # Keep the order the resume already has: new assembly orders skills by
        # the job, and re-sorting here put the AI tooling rows first in every DOCX.
    return parsed
