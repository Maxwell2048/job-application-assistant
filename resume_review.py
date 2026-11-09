"""Review and reorder existing evidence-backed project blocks for a job."""
import copy

from local_llm import LLMError

PROMPT = """Review the resume projects against the job description. Return every supplied project ID exactly once, ordered from most to least relevant. Do not invent or rewrite any facts. Ignore seniority, company name and keywords without supporting evidence. Provide a short English reason for the ordering."""
SCHEMA = {"type": "object", "required": ["order", "reason"], "additionalProperties": False,
          "properties": {"order": {"type": "array", "items": {"type": "string"}},
                         "reason": {"type": "string", "minLength": 1}}}


def review_projects(llm, sections, jd_text, timeout_seconds=None):
    """Reorder whole existing blocks only. Never modify text, citations or membership."""
    result = copy.deepcopy(sections)
    targets = []
    for idx, section in enumerate(result):
        if "PROJECT" not in section.get("title", "").upper():
            continue
        lines = section.get("lines", [])
        starts = [i for i, line in enumerate(lines) if line.get("kind") == "heading"]
        if len(starts) < 2 or starts[0] != 0:
            continue
        blocks = [lines[start:(starts[n + 1] if n + 1 < len(starts) else len(lines))]
                  for n, start in enumerate(starts)]
        targets.append((idx, blocks))
    if not targets:
        return result, ["AI project ordering review skipped: no project section with at least two project blocks."]
    notes = []
    for index, blocks in targets:
        ids = [f"P{i+1}" for i in range(len(blocks))]
        payload = {"job_description": jd_text[:6000], "projects": [
            {"id": ids[i], "heading": b[0].get("text", ""),
             "bullets": [line.get("text", "") for line in b[1:]]} for i, b in enumerate(blocks)]}
        def validate(data):
            order = data.get("order", [])
            return [] if len(order) == len(ids) and set(order) == set(ids) else ["The order must contain each provided project ID exactly once."]
        try:
            review = llm.generate_json("review_resume_projects", PROMPT, payload, SCHEMA,
                                       timeout_seconds, validate=validate)
            if validate(review):
                raise ValueError("Invalid project order")
            by_id = dict(zip(ids, blocks))
            result[index]["lines"] = [line for key in review["order"] for line in by_id[key]]
            before = [block[0].get("text", "") for block in blocks]
            after = [by_id[key][0].get("text", "") for key in review["order"]]
            notes.append("AI project ordering review: " + review["reason"][:500])
            notes.append("Project order before review: " + " | ".join(before))
            notes.append("Project order after review: " + " | ".join(after))
        except (LLMError, ValueError, KeyError, TypeError) as exc:
            notes.append("AI project ordering review unavailable; original order retained: " + str(exc)[:240])
    return result, notes
