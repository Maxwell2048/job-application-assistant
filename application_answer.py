"""Answer an application-form question for a job, from verified material.

    python application_answer.py --run outputs/<run> --resume resume/base_resume.docx \
        --config config.local.json --request answer_request.json

The request file holds {"question": ..., "max_words": 250}. The answer is
written in English and cites material like the cover letter does (verbatim
quotes, numbers present in the material, at least one personal source, no
internal review wording). Facts the candidate must confirm themselves
(clearances, licences, availability...) are listed instead of asserted.
Appends to application_answers.json in the run directory.
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

from local_llm import LLMError, build_llm, load_config
from tailor_cv import (CVTailor, SYSTEM_PROMPT, _attach_sources, latest_output, load_materials, load_run,
                       read_resume, requirement_summary, validate_basis)

ANSWERS_FILE = "application_answers.json"
DEFAULT_WORDS = 250
SCHEMA = {
    "type": "object", "required": ["answer", "to_confirm"], "additionalProperties": False,
    "properties": {
        "answer": {"type": "array", "minItems": 1, "maxItems": 6, "items": {
            "type": "object", "required": ["text", "basis"], "additionalProperties": False,
            "properties": {"text": {"type": "string", "minLength": 1},
                           "basis": {"type": "array", "minItems": 1, "items": {
                               "type": "object", "required": ["material_id", "quote"], "additionalProperties": False,
                               "properties": {"material_id": {"type": "string"},
                                              "quote": {"type": "string", "minLength": 1}}}}}}},
        "to_confirm": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
    },
}


def word_count(paragraphs):
    return sum(len(p.get("text", "").split()) for p in paragraphs)


def trim_to_limit(paragraphs, max_words):
    """After a retry, drop whole trailing paragraphs (never the first) until the
    answer fits: every kept sentence stays as written and cited."""
    while len(paragraphs) > 1 and word_count(paragraphs) > max_words:
        paragraphs.pop()
    return paragraphs


def answer_question(llm, tailor, suggestions, matches, jd_text, question, max_words=DEFAULT_WORDS,
                    timeout_seconds=None):
    requirements = requirement_summary(matches)
    target = max(30, int(max_words * 0.8))   # local models overshoot word counts; aim below the limit
    note = (f"回答求职申请表里的问题：“{question}”。用第一人称英文回答，1–4 段，每段 2–3 句，总共约 {target} 个英文单词"
            f"（硬上限 {max_words}）；"
            "直接回答问题本身，用 tailored_bullets 和材料里与岗位最相关的经历举例，交代做了什么、怎么做、结果。"
            "材料不支持的经历不写；可以坦诚说明尚无某类正式经验，但不要罗列缺口。"
            "to_confirm 用中文列出回答涉及、但需要本人确认的事实（如无犯罪记录证明、驾照、可入职时间、签证细节）；"
            "这些事实不要写成已具备。每段给出 basis。")
    payload, allowed = tailor._closing_request("application_answer", note, SCHEMA, requirements,
                                               suggestions.get("projects", []), suggestions.get("gaps", []), jd_text)

    checks = {"count": 0}

    def check(data):
        checks["count"] += 1
        paragraphs = data.get("answer", [])
        if checks["count"] > 1:
            trim_to_limit(paragraphs, max_words)
        errors = validate_basis(paragraphs, tailor.by_id, allowed, label="answer 第 {index} 段", jd_text=jd_text,
                                require_personal=True, no_course_codes=True, prune_quotes=True,
                                english_only=True, allow_jd_names=True)
        words = word_count(paragraphs)
        if words > max_words:
            errors.append(f"回答共 {words} 个单词，超过上限 {max_words}；删掉一段或把每段压缩到 2 句，总共约 {target} 词")
        return errors
    data = llm.generate_json("application_answer", SYSTEM_PROMPT, payload, SCHEMA, timeout_seconds,
                             validate=check)
    return {"answer": [_attach_sources(p, tailor.by_id) for p in data["answer"]],
            "to_confirm": [t for t in data.get("to_confirm", []) if t.strip()],
            "words": word_count(data["answer"])}


def save(run_dir, entry):
    path = Path(run_dir) / ANSWERS_FILE
    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    except ValueError:
        existing = []
    existing.append(entry)
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="根据已核对材料回答申请表问题")
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--resume", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--request", required=True, type=Path, help='JSON：{"question": ..., "max_words": 250}')
    parser.add_argument("--data", type=Path, default=Path(__file__).resolve().parent / "experiences")
    args = parser.parse_args(argv)
    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        question = str(request["question"]).strip()
        max_words = int(request.get("max_words") or DEFAULT_WORDS)
        meta, matches, jd_text = load_run(args.run)
        suggestions_path = latest_output(args.run, "cv_suggestions", ".json")
        suggestions = json.loads(suggestions_path.read_text(encoding="utf-8")) if suggestions_path else {}
        config = load_config(args.config)
        llm = build_llm(config)
        resume_text = read_resume(args.resume)
    except (OSError, ValueError, KeyError, LLMError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    materials, by_project, resume_entry = load_materials(args.data, resume_text)
    tailor = CVTailor(llm, materials, by_project, resume_entry, config.get("timeout_seconds"))
    entry = {"id": request.get("id") or datetime.datetime.now().strftime("%Y%m%d%H%M%S"),
             "question": question, "max_words": max_words,
             "created_at": datetime.datetime.now().isoformat(timespec="seconds")}
    try:
        entry.update(status="complete", **answer_question(llm, tailor, suggestions, matches, jd_text, question,
                                                          max_words, config.get("timeout_seconds")))
    except LLMError as exc:
        entry.update(status="error", error=str(exc))
    save(args.run, entry)
    print(f"回答：{entry['status']}" + (f"（{entry['words']} 词）" if entry.get("words") else ""))
    if entry["status"] != "complete":
        print(entry.get("error", ""), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
