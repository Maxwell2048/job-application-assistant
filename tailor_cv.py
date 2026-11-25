"""Tailor a base CV to one JD using a finished match_job run.

    python tailor_cv.py --run outputs/<run> --resume resume/base_resume.docx --config config.local.json

Everything the model writes must be a rewrite of existing material: verified
experience sections (experiences/*.md), self-declared profile facts, or the
base resume text. Each bullet/paragraph cites a material id plus a verbatim
quote; the program checks the quote is a substring of that material and that
every number in the bullet appears in the project's material. Quotes that
fail are sent back as validation errors within the adapter's retry budget.

Outputs cv_suggestions.md and cv_suggestions.json in the run directory.
Nothing here is a final CV: it is an evidence-linked draft for human review.
"""
import argparse
import datetime
import json
import re
import sys
import zipfile
from html import unescape
from pathlib import Path

from job_family import job_family, job_title
from local_llm import LLMError, build_llm, load_config
from search_experience import read_sections

PROJECT_ROOT = Path(__file__).resolve().parent
TAILOR_PROMPT_VERSION = "10"
PROFILE_FILE = "profile.md"
RESUME_ID = "RESUME"
UNVERIFIED_MARK = re.compile(r"事实状态\s*[:：]\s*待核对")
EVIDENCE_KINDS = ("experience", "unverified")
# Sections that describe the candidate's own work. Everything else in an
# experience file (项目背景, 项目内容与团队成果, 基本信息, 证据边界...) is
# context: it may be cited alongside, never as a bullet's only basis.
PERSONAL_SECTION = re.compile(
    r"^(我的贡献|我的工作|个人贡献|个人工作|本人自述|STAR|英文简历表述|My Contributions?\b|My Work\b)", re.I)
MAX_BULLETS = 4
MAX_BULLET_CHARS = 360
ACTIONS = ["keep", "move_up", "move_down", "trim", "remove"]

# ---------------------------------------------------------------- inputs


def read_resume(path):
    """Plain text of a .docx (paragraphs in document order, <w:br/> as newline),
    or a UTF-8 .md/.txt file."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt", ".markdown"}:
        return path.read_text(encoding="utf-8-sig")
    if suffix != ".docx":
        raise ValueError(f"底稿必须是 .docx、.md 或 .txt：{path}")
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    paragraphs = []
    for para in re.findall(r"<w:p(?:\s[^>]*)?>.*?</w:p>", xml, flags=re.S):
        para = re.sub(r"<w:(?:br|cr)\s*/>", "\n", para)
        para = re.sub(r"<w:tab\s*/>", "\t", para)
        runs = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>|(\n)", para, flags=re.S)
        text = "".join(unescape(t) if t else nl for t, nl in runs)
        if text.strip():
            paragraphs.append(text.strip())
    return "\n".join(paragraphs) + "\n"


def load_run(run_dir):
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
    matches = json.loads((run_dir / "matches.json").read_text(encoding="utf-8"))["matches"]
    jd_text = (run_dir / "jd.txt").read_text(encoding="utf-8")
    if meta.get("scope") != "full":
        raise ValueError(f"运行 {run_dir.name} 只做了提取（scope={meta.get('scope')}），"
                         "先完整运行 match_job.py 再定制简历")
    return meta, matches, jd_text


# Lines that record where a fact came from (证据：…, import notes) keep the library
# auditable; shown to the model they were translated into resumes as "self-reported,
# no personal commits". Boundary sections say what must not be claimed; given as
# citable material they became bullets about what was not done.
INTERNAL_LINE = re.compile(r"^\s*(?:[-*]\s*)?(?:证据\s*[:：]|本文件由|主要交付文件|成绩缺失)")
BOUNDARY_SECTION = re.compile(r"^(?:证据边界|当前信息边界)")


def model_text(text):
    """Material text as the model sees it: provenance lines removed. Quotes are
    still verified against the full text, of which this is a line subset."""
    return "\n".join(line for line in (text or "").splitlines() if not INTERNAL_LINE.match(line)).strip()


def is_boundary(entry):
    return bool(BOUNDARY_SECTION.match(entry.get("section", "")))


def visible_materials(entries, with_project=False):
    """Citable materials for a prompt, without provenance lines or empty sections."""
    out = []
    for e in entries:
        text = model_text(e["text"])
        if text:
            item = {"material_id": e["material_id"], "kind": e["kind"], "scope": e.get("scope", "personal"),
                    "section": e["section"], "text": text}
            out.append({**item, "project": e["project"]} if with_project else item)
    return out


# What to lead with for a job family. Only the emphasis changes: the facts, ownership
# and limits of each project stay as the material states them.
FAMILY_FOCUS = {
    "it": ("目标岗位是 IT 支持或系统管理类：每个项目优先写与支持工作相通的、材料确有的内容——"
           "定位并解决问题、部署与测试、维护与备份恢复、记录与文档、权限与工单流程、与用户或非技术人员沟通、"
           "根据用户反馈改进；用这些动作开头，技术细节放在后面。仍只写材料支持的事实，"
           "不把个人或课程项目写成在职的 IT 支持工作，不新增工单量、用户数或响应时间等数字。"),
}


WORK_TYPE = re.compile(r"经历类型\s*[:：]\s*工作经历")
MAX_TAILORED_PROJECTS = 6   # a resume shows 4-6 projects; writing bullets for the rest only costs time


def is_work(entries):
    """True for an experience file marked 经历类型：工作经历 (a job, not a project)."""
    return any(WORK_TYPE.search(e.get("text", "")) for e in entries)


def tailored_projects(order, by_project, limit=MAX_TAILORED_PROJECTS):
    """The projects to write bullets for: the `limit` with the most evidence for
    this JD, plus every job held (work experience always appears)."""
    picked = [item for item in order if not is_work(by_project.get(item["project"], []))][:limit]
    picked += [item for item in order if is_work(by_project.get(item["project"], []))]
    keep = {item["project"] for item in picked}
    return [item for item in order if item["project"] in keep]


def load_materials(data_dir, resume_text=None):
    """Numbered material entries grouped by project; the resume (if given) is
    one extra entry, otherwise resume_entry is None."""
    materials, by_project = [], {}
    records = read_sections(Path(data_dir))
    # A file whose 基本信息 says 事实状态：待核对 is resume-derived, not verified.
    unverified_sources = {r["source"] for r in records
                          if UNVERIFIED_MARK.search(r["text"])}
    for index, record in enumerate(records, 1):
        if Path(record["source"]).name == PROFILE_FILE:
            kind = "profile"
        elif record["source"] in unverified_sources:
            kind = "unverified"
        else:
            kind = "experience"
        entry = {
            "material_id": f"M{index:03d}",
            "kind": kind,
            "scope": "personal" if kind == "profile" or PERSONAL_SECTION.match(record["section"])
            else "team",
            "project": record["project"],
            "section": record["section"],
            "source": record["source"],
            "line_start": record["line_start"],
            "line_end": record["line_end"],
            "text": record["text"],
        }
        materials.append(entry)
        by_project.setdefault(record["project"], []).append(entry)
    if resume_text is None:
        return materials, by_project, None
    resume_entry = {"material_id": RESUME_ID, "kind": "resume", "scope": "personal",
                    "project": "简历底稿", "section": "全文", "source": "resume", "line_start": 1,
                    "line_end": resume_text.count("\n") + 1, "text": resume_text}
    materials.append(resume_entry)
    return materials, by_project, resume_entry


# ---------------------------------------------------------------- analysis


def requirement_summary(matches):
    return [{
        "requirement_id": m["requirement_id"],
        "text": m["text"],
        "category": m["category"],
        "importance": m["importance"],
        "verdict": m["verdict"] if m["processing_status"] == "ok" else "unprocessed",
        "missing_aspects": m.get("missing_aspects", []),
        "evidence_projects": sorted({e["project"] for e in m.get("evidence", [])}),
    } for m in matches]


def rank_projects(matches, by_project):
    """Projects ordered by how much verified evidence they gave this JD."""
    scores = {name: 0 for name in by_project}
    weight = {"direct": 3, "related": 1}
    for match in matches:
        if match["processing_status"] != "ok":
            continue
        for evidence in match.get("evidence", []):
            project = evidence["project"]
            if project in scores and Path(evidence["source"]).name != PROFILE_FILE:
                scores[project] += weight.get(match["verdict"], 0)
    ordered = sorted(scores, key=lambda p: (-scores[p], p))
    return [{"project": p, "evidence_score": scores[p]} for p in ordered
            if any(e["kind"] in EVIDENCE_KINDS for e in by_project[p])]


def gaps(matches):
    """Requirements the material cannot support; never to be written as claims."""
    out = []
    for m in matches:
        if m["processing_status"] != "ok":
            out.append({**_gap_base(m), "reason": "处理未完成", "kind": "unprocessed"})
        elif m["verdict"] == "insufficient":
            out.append({**_gap_base(m), "reason": m.get("reason", ""), "kind": "insufficient"})
        elif m["verdict"] == "related" and m.get("missing_aspects"):
            out.append({**_gap_base(m), "reason": "；".join(m["missing_aspects"]),
                        "kind": "partial"})
    return out


def _gap_base(m):
    return {"requirement_id": m["requirement_id"], "text": m["text"],
            "importance": m["importance"]}


# ---------------------------------------------------------------- validation

NUMBER = re.compile(r"\d[\d,.]*\d|\d")
# University unit codes (CITS5505, GENG5505...) read as student work on a resume.
COURSE_CODE = re.compile(r"\b[A-Z]{4}\d{4}\b")


_LOOSE = re.compile(r"[\s\-–—_/,.;:()\[\]\"'“”‘’\u3001\uff0c\u3002\uff1b\uff1a\uff08\uff09]+")


def _loose(text):
    return _LOOSE.sub("", text).casefold()


def quote_in(quote, text):
    """Verbatim substring, or equal after ignoring case, whitespace and
    punctuation (so 'problem-solving' matches 'problem solving'). Words must
    still appear in order and in full."""
    if not quote or not quote.strip():
        return False
    if quote in text:
        return True
    pieces = [p for p in _ELISION.split(quote) if p.strip()]
    if len(pieces) > 1:
        # 'A...B': an elided quote counts when every fragment is verbatim source
        # text, the fragments appear in order, and one of them is long enough to
        # anchor the quote (short connecting words such as 字段 are allowed).
        if max(len(_loose(p)) for p in pieces) < MIN_ELIDED_CHARS:
            return False
        haystack, start = _loose(text), 0
        for piece in pieces:
            found = haystack.find(_loose(piece), start)
            if found < 0:
                return False
            start = found + len(_loose(piece))
        return True
    loose_quote = _loose(quote)
    return bool(loose_quote) and loose_quote in _loose(text)


_ELISION = re.compile(r"\s*(?:\.{3,}|…+)\s*")
MIN_ELIDED_CHARS = 8


_CJK = re.compile(r"[\u4e00-\u9fff]")
# Chinese, Japanese and Korean text plus full-width punctuation such as ：（），
NON_ENGLISH = re.compile(r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af"
                         r"\uf900-\ufaff\uff00-\uffef]+")
MAX_TITLE_CHARS = 90


def non_english(text):
    """The first run of CJK characters or full-width punctuation in text, or None."""
    match = NON_ENGLISH.search(text or "")
    return match.group(0) if match else None


# Review vocabulary of the evidence library (自述 / 待核对 / 无本人提交 ...). It keeps
# ownership honest internally, but a recruiter reads it as doubt about the
# candidate, and remarks about the old resume's errors read as past misstatements.
INTERNAL_WORDING = re.compile(
    r"\bself[- ]reported\b|\bself[- ]declared\b|\bcandidate[- ]declared\b|\buser[- ]declared\b"
    r"|\bunverified\b|\bverified (?:evidence|facts?|experience|sources?)\b|\bverified,\s*self"
    r"|\bno (?:personal|own) commits?\b|\bwithout (?:personal|own) commits?\b"
    r"|\bno per[- ]module\b|\bno recorded module split\b|\bindividual module (?:ownership|split)\b"
    r"|\bownership (?:was |is )?not recorded\b|\bnot (?:separately )?attributed\b"
    r"|\bcannot be attributed\b|\bno (?:file|documentary) (?:evidence|support)\b"
    r"|\b(?:old|prior|previous|earlier|stale|original) (?:base )?resume\b"
    r"|\bcorrect\w* (?:the |a |prior |old )?resume drafts?\b|\bresume drafts? discrepanc|\bagainst (?:the )?resume drafts?\b"
    r"|\bresume (?:discrepanc|inaccurac|claims?\b|errors?\b)"
    r"|\bphantom\b|\bnon-existent (?:WebSocket|Selenium|features?)\b|\bdraft facts?\b", re.I)


# '而非' translated as 'rather predictive accuracy': every 'rather' without 'than'
# in November's 6,454 generated texts was this error.
RATHER_WITHOUT_THAN = re.compile(r"\brather\b(?!\s+than\b)\s+\S+", re.I)


# Administrator-side or end-user-support claims. The profile states user-level
# Microsoft 365 / Windows only and no support-desk work; building login pages is
# not access management. Resume text only: a cover letter may name such gaps honestly.
# Building a ticketing system (SupportOps AI) is evidenced, so 'ticketing' is not
# flagged; working on a service desk or help desk is not.
ADMIN_CLAIM = re.compile(
    r"\b(?:user access management|access management|identity (?:and access )?management|"
    r"manag\w* (?:user )?(?:access|accounts?|permissions|identit\w+)|administer\w* (?:user )?accounts?|"
    r"account administration|end[- ]user(?:-facing)? (?:application )?support|support\w* end[- ]users?\b|"
    r"end[- ]user needs|Windows Server|Active Directory|Entra(?: ID)?|Intune|Exchange (?:Online|Server)|"
    r"help ?desk|service desk)\b", re.I)
ADMIN_CLAIM_HINT = ("材料没有管理员侧或终端用户支持经历：认证功能写成 “built login/registration pages and "
                    "authentication routes”；不要写 managing user access、identity management、end-user support、"
                    "service desk，也不要写 Active Directory、Entra ID、Intune、Windows Server 等未使用过的管理员工具")


# Capitalised names (platforms, tools, products, roles) taken from the JD but absent
# from every material: 'comfortable across Windows, macOS, iOS and Android' was
# written for a JD that listed them. Quote checks miss it because the quotes
# support the other clauses.
_NAME_TOKEN = re.compile(r"(?<![\w.])([A-Z][A-Za-z0-9+#]*(?:[.\-][A-Za-z0-9+#]+)*|[a-z]+[A-Z][A-Za-z0-9]*)(?![\w])")
_NAME_OK = {"us", "uk", "ui", "ux", "api", "apis", "it", "ict", "wa", "uwa"}
_corpus_cache = {}


def _word_in(word, text_cf):
    return re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", text_cf) is not None


def jd_only_names(text, jd_text, materials_by_id):
    """Capitalised names in text that the JD uses but no material mentions."""
    key = id(materials_by_id)
    if key not in _corpus_cache:
        _corpus_cache.clear()
        _corpus_cache[key] = "\n".join(m.get("text", "") for m in materials_by_id.values()).casefold()
    corpus, jd = _corpus_cache[key], (jd_text or "").casefold()
    found = []
    for match in _NAME_TOKEN.finditer(text or ""):
        token = match.group(1)
        before = text[:match.start()].rstrip()
        if len(token) < 2 or not before or before.endswith((".", ":", ";", "!", "?")):
            continue  # sentence-initial capitals are ordinary words
        word = token.casefold()
        stems = {word, re.sub(r"-(?:based|driven|backed|style|level)$", "", word)}
        stems |= {word[:-len(suffix)] for suffix in ("n", "an", "ian", "s") if word.endswith(suffix)}
        if word in _NAME_OK or not _word_in(word, jd) or any(_word_in(s, corpus) for s in stems if len(s) >= 2):
            continue
        found.append(token)
    return list(dict.fromkeys(found))


def internal_wording(text):
    """The first internal evidence-review phrase in resume text, or None."""
    match = INTERNAL_WORDING.search(text or "")
    return match.group(0) if match else None


MIN_PREFIX_CHARS = 20


def verbatim_prefix(quote, text):
    """The longest leading part of quote that is verbatim in text (ignoring case,
    spacing and punctuation, as quote_in does), as original quote characters."""
    loose_text = _loose(text)
    best, built = "", ""
    for index, char in enumerate(quote):
        built += _loose(char)
        if built and built not in loose_text:
            break
        best = quote[:index + 1]
    return best.rstrip(" ，,、；;：:（(→-") if _loose(best) else ""


def divergence_hint(quote, text):
    """Where a near-verbatim quote departs from the material, for the retry message."""
    prefix = verbatim_prefix(quote, text)
    if len(_loose(prefix)) < MIN_PREFIX_CHARS:
        return ""
    loose_text, loose_prefix = _loose(text), _loose(prefix)
    at = loose_text.find(loose_prefix) + len(loose_prefix)
    rest = quote[len(prefix):].strip()[:20]
    return (f"（前半段 {prefix[-20:]!r} 与原文一致；之后写成了 {rest!r}，原文此处接着是 "
            f"{loose_text[at:at + 20]!r}：不要省略中间的内容，或只引用一致的那一段）")


def shorten_title(title, limit=None):
    """Drop trailing ' | ' segments until the heading fits; None when it cannot."""
    limit = limit or MAX_TITLE_CHARS
    if not isinstance(title, str) or len(title) <= limit:
        return title
    parts = [p.strip() for p in re.split(r"\s*\|\s*", title) if p.strip()]
    while len(parts) > 1 and len("  |  ".join(parts)) > limit:
        parts.pop()
    joined = "  |  ".join(parts)
    return joined if len(joined) <= limit else title


def trim_quotes_to_verbatim(bullets, materials_by_id, allowed_ids):
    """After a retry, cut a quote that is verbatim up to some point back to that
    point (at least MIN_PREFIX_CHARS and half the quote). What is shown stays
    real source text; a quote that is wrong from the start is left for the check."""
    for b in bullets:
        for basis in (b.get("basis") or []) if isinstance(b, dict) else []:
            mid, quote = basis.get("material_id"), basis.get("quote") or ""
            if mid not in allowed_ids or quote_in(quote, materials_by_id[mid]["text"]):
                continue
            prefix = verbatim_prefix(quote, materials_by_id[mid]["text"])
            if len(_loose(prefix)) >= MIN_PREFIX_CHARS and len(_loose(prefix)) * 2 >= len(_loose(quote)):
                basis["quote"] = prefix


def repair_after_retry(bullets):
    """Second chance for wording a retry did not fix, without losing the project.

    'rather X' becomes 'rather than X' (the only meaning seen in practice);
    bullets that still carry internal review wording are dropped as long as
    another bullet remains. Returns how many bullets were dropped."""
    for b in bullets:
        if isinstance(b, dict) and isinstance(b.get("text"), str):
            b["text"] = re.sub(r"\brather\b(?!\s+than\b)", "rather than", b["text"], flags=re.I)
    keep = [b for b in bullets if not (isinstance(b, dict) and internal_wording(b.get("text", "")))]
    if keep and len(keep) < len(bullets):
        dropped = len(bullets) - len(keep)
        bullets[:] = keep
        return dropped
    return 0


INTERNAL_WORDING_HINT = ("这是内部核对用语，不能写进投递材料。归属用自然的简历表述保留："
                         "协助类写 “supported the team's … module”，只有材料写明两人项目时才写 “co-developed … with a teammate”，多人小组写 “with the team”，团队规模照材料写；"
                         "不要写自述/核对状态/有无本人提交，也不要提旧简历的错误或已删除的功能；"
                         "知识库整理可以写 “with source-linked citations for every generated claim”")


def english_title_errors(title, project):
    """A resume heading for a project whose material title is in Chinese."""
    if not isinstance(title, str) or not title.strip():
        return [f"缺少 title：为项目“{project}”给出英文简历标题，格式如 “Project Name  |  Type”"]
    errors = []
    if non_english(title):
        errors.append(f"title 含中文或全角字符 {non_english(title)!r}：简历标题必须全英文，"
                      "把项目名意译成英文，如 “Job Evidence RAG  |  LLM Application”")
    if len(title) > MAX_TITLE_CHARS:
        errors.append(f"title 有 {len(title)} 个字符，超过 {MAX_TITLE_CHARS}；只保留项目名与类型")
    return errors


def looks_translated(quote, material_text):
    """An all-English quote offered for a material written mainly in Chinese:
    the model translated the sentence instead of copying it."""
    if _CJK.search(quote) or not material_text:
        return False
    return len(_CJK.findall(material_text)) > 0.3 * len(material_text.replace(" ", ""))


def personal_ids(materials_by_id, allowed_ids):
    return {i for i in allowed_ids if materials_by_id[i].get("scope", "personal") == "personal"}


def validate_basis(items, materials_by_id, allowed_ids, text_key="text",
                   label="第 {index} 条", jd_text=None, require_personal=False,
                   no_course_codes=False, allow_jd_names=False, prune_quotes=False, max_chars=None,
                   english_only=False, resume_text=False):
    """Every item must cite existing materials with verbatim quotes, and every
    number in its text must appear in the cited materials (or the allowed
    material pool for that call). A quote that is JD wording rather than
    material text is named as such so the retry can correct it. With
    require_personal, an item citing only team/background sections is rejected;
    with no_course_codes, resume text containing unit codes is rejected; with
    allow_jd_names, a number inside a product or standard name taken from the
    JD ('Microsoft 365') is not treated as an invented figure. With prune_quotes,
    a citation that fails the verbatim check is removed from the item (in place)
    as long as another valid citation remains, the way the matcher drops bad
    quotes: one slip in a long letter should not void the whole text, and what
    is kept and shown is still only verified source text."""
    errors = []
    pool_text = "\n".join(materials_by_id[i]["text"] for i in allowed_ids)
    own = personal_ids(materials_by_id, allowed_ids)
    for index, item in enumerate(items, 1):
        who = label.format(index=index)
        text = item.get(text_key, "")
        if not isinstance(text, str) or not text.strip():
            errors.append(f"{who} {text_key} 为空")
            continue
        if no_course_codes and COURSE_CODE.search(text):
            errors.append(f"{who}含课程代码 {COURSE_CODE.search(text).group(0)}；简历内容不写课程代码，"
                          "删除它或改为项目性质描述（如 individual project / group project）")
        if english_only and non_english(text):
            errors.append(f"{who}含中文或全角字符 {non_english(text)!r}；简历和求职信必须全英文"
                          "（quote 保持材料原文语言，但正文与标题一律写英文）")
        if english_only and internal_wording(text):
            errors.append(f"{who}含 {internal_wording(text)!r}：{INTERNAL_WORDING_HINT}")
        if resume_text and jd_text and jd_only_names(text, jd_text, materials_by_id):
            names = "、".join(jd_only_names(text, jd_text, materials_by_id))
            errors.append(f"{who}写了 {names}：这些名称只出现在 JD 里，材料中没有相关经历；删掉，"
                          "只写材料支持的工具、平台和经历（不复述岗位名称或公司名）")
        if resume_text and ADMIN_CLAIM.search(text):
            errors.append(f"{who}含 {ADMIN_CLAIM.search(text).group(0)!r}：{ADMIN_CLAIM_HINT}")
        if english_only and RATHER_WITHOUT_THAN.search(text):
            # The only meaning seen in practice is a contrast: repair it, do not spend a retry.
            text = item[text_key] = re.sub(r"\brather\b(?!\s+than\b)", "rather than", text, flags=re.I)
        if max_chars and len(text) > max_chars:
            errors.append(f"{who}有 {len(text)} 个字符，超过上限 {max_chars}；请压缩")
        basis = item.get("basis") or []
        if not basis:
            errors.append(f"{who}缺少 basis 来源引用")
        valid, quote_errors = [], []
        for b in basis:
            mid, quote = b.get("material_id"), b.get("quote", "")
            if quote and (mid not in allowed_ids or not quote_in(quote, materials_by_id[mid]["text"])):
                # A verbatim quote filed under the wrong id (usually base-resume text cited
                # as a project section): correct the id when exactly one allowed material has it.
                owners = [i for i in allowed_ids if quote_in(quote, materials_by_id[i]["text"])]
                if len(owners) == 1:
                    b["material_id"] = mid = owners[0]
            if mid not in allowed_ids:
                quote_errors.append(f"{who}引用了不存在或本次不可用的 material_id {mid!r}")
            elif not quote or not quote_in(quote, materials_by_id[mid]["text"]):
                hint = ""
                if jd_text and quote and quote.lower() in jd_text.lower():
                    hint = "（这是 JD 的措辞，不是材料原文；quote 只能摘自 materials/profile/resume_draft）"
                elif quote and looks_translated(quote, materials_by_id[mid]["text"]):
                    hint = "（该材料是中文，quote 却是英文：不要翻译，直接复制材料里的中文原句；正文可以用英文写）"
                elif quote:
                    hint = divergence_hint(quote, materials_by_id[mid]["text"])
                quote_errors.append(f"{who}对 {mid} 的 quote 不是该材料的逐字连续原文："
                                    f"{quote[:60]!r}{hint}")
            else:
                valid.append(b)
        if prune_quotes and valid and quote_errors:
            item["basis"] = basis = valid
        else:
            errors += quote_errors
        if basis and require_personal and not any(b.get("material_id") in own for b in basis):
            errors.append(f"{who}只引用了 scope=team 的项目背景/团队成果章节；"
                          "必须至少引用一处 scope=personal 的本人材料（我的贡献/我的工作/STAR/英文简历表述/自述/profile）")
        for number in NUMBER.findall(text):
            if allow_jd_names and jd_name_number(number, text, jd_text):
                continue
            if not number_supported(number, pool_text):
                errors.append(f"{who}包含材料中没有的数字 {number!r}；"
                              "不要新增或换算数字")
    return errors


_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")


def number_supported(number, pool_text):
    """The number appears in the material, allowing only a difference in
    thousands separators ('1,288' vs '1288'). No rounding or unit conversion."""
    if number in pool_text:
        return True
    plain = _THOUSANDS.sub("", number)
    return plain in _THOUSANDS.sub("", pool_text)


_JD_NAME = re.compile(r"\b[A-Z][A-Za-z]+[ \-](\d[\d,.]*\d|\d)\b"
                      r"(?!\s*(?:\+|%|years?|yrs?|months?|weeks?|days?|hours?)\b)")


def jd_name_number(number, text, jd_text):
    """True when the number is part of a name the JD itself uses, such as
    'Microsoft 365' or 'ISO 27001', and the text repeats that exact name.
    Quantities like '5 years' never qualify."""
    if not jd_text:
        return False
    lowered = text.casefold()
    return any(match.group(1) == number and match.group(0).casefold() in lowered
               for match in _JD_NAME.finditer(jd_text))


def validate_quotes_in(items, text, key="quote"):
    return [f"第 {i} 条 {key} 不是简历底稿的逐字原文：{item.get(key, '')[:60]!r}"
            for i, item in enumerate(items, 1)
            if not item.get(key) or item[key] not in text]


# ---------------------------------------------------------------- prompts

SYSTEM_PROMPT = """你为求职者定制英文简历内容，读者是招聘方的 HR 和用人经理。事实以材料为准：可以概括、合并同一项目的多处事实、解释这项工作的用途和对岗位的意义，
用招聘方看得懂的普通英文表达；但不得新增材料里没有的工具、数字、雇主、职位、职责范围、用户规模或成果。
材料分三类：experience（已核对的项目经历）、profile（本人自述的基本事实）、resume（现有简历底稿，未经证据核对）。
每条材料还有 scope：personal 是本人的工作或自述，team 是项目背景或团队成果。团队成果不是本人成果：
要点可以引用 team 材料交代背景，但每条要点必须至少引用一处 personal 材料，且不能把 team 材料里的工作写成本人完成。
每条输出都要在 basis 里给出至少一处 material_id 和该材料中逐字连续的 quote，说明主要依据；措辞不必逐句对应材料，可以精简、换序、概括和解释，但不能夸大。
JD 要求列表里 verdict=insufficient 的要求没有证据支持，不要在简历中声称具备；verdict=related 的要求只能写材料实际覆盖的部分。
语言风格：专业、清楚、让非技术读者也能看懂。每条先用一句话说明做了什么、为谁或为什么做、带来什么用处（如 so staff can…、making records reliable），再补一两个关键技术；不要堆砌技术名词，完整工具清单留给技能栏。突出可迁移的能力（解决问题、可靠的数据和记录、与用户沟通、交付可用的系统），不写成任务流水账。
可以强化动词、适度使用 clear、reliable、practical 这类平实的正面词，说明工作的实际价值；但不写 significantly improved、proven、expert 这类无法证实的程度或效果，不虚构质量提升、领导职责、独立所有权、商业规模或量化结果。提交次数和标注数量作为工作范围的补充，不作为能力描述的主体。
新增本人确认技能（2026-10-23）：Hugging Face Transformers、Ollama、LoRA、模型微调；按 profile 自述引用，不推断微调规模或实际效果。
技能展示偏好（2026-10-23）：技能清单不列 YOLO 系列；项目正文中有证据的 YOLO 训练描述仍保留。Harness Engineering、MCP（Model Context Protocol）为本人新确认的技能，只引用 profile 自述，不扩写为未提供证据的项目、年限或生产成果。
LangChain、LangGraph 为本人确认技能（2026-11-25）：用于 Job Evidence RAG 的模型接入、检索和生成流程编排，可在该项目要点中按材料引用；不写 LangChain Agent、工具调用、LangSmith、多 Agent 或生产部署。
简历正文不出现课程代码（CITS5505 之类），用 individual project / group project 等描述项目性质。
材料里的“本人自述”“待核对”“无本人提交/无文件佐证”“没有模块分工”“旧简历与证据不符、已删除的功能”是内部核对记录，只用来决定怎么写归属，不写进简历和求职信：
协助类工作写成 supported/assisted the team's … module；材料写明两人项目时写 co-developed … with a teammate，多人小组写 with the team（团队人数按材料，不得改写；同一项目的归属说明写一次即可，不要每条要点重复）；不写 self-reported、unverified、no personal commits、cannot be attributed 等核对用语，也不提旧简历的错误。
摘要针对岗位方向选取最相关的证据，不罗列与岗位无关的 AI 方向清单；不在摘要或要点里点名材料不支持的工具或缺口（如 “eager to learn MYOB”）。
简历正文用英文；reason/note 字段用中文。输出符合 schema 的紧凑 JSON（不缩进、不换行、无代码围栏）。"""

BULLETS_SCHEMA = {
    "type": "object",
    "required": ["title", "bullets"],
    "additionalProperties": False,
    "properties": {
        # length is checked (and long titles shortened) by the program, not the schema
        "title": {"type": "string", "minLength": 1},
        "bullets": {
            "type": "array",
            "maxItems": MAX_BULLETS,
            "items": {
                "type": "object",
                "required": ["text", "basis", "requirement_ids"],
                "additionalProperties": False,
                "properties": {
                    "text": {"type": "string", "minLength": 1, "maxLength": MAX_BULLET_CHARS},
                    "basis": {"type": "array", "minItems": 1, "items": {
                        "type": "object", "required": ["material_id", "quote"],
                        "additionalProperties": False,
                        "properties": {"material_id": {"type": "string"},
                                       "quote": {"type": "string", "minLength": 1}}}},
                    "requirement_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "note": {"type": "string"},
    },
}

REVIEW_SCHEMA = {
    "type": "object",
    "required": ["items"],
    "additionalProperties": False,
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "required": ["quote", "action", "reason"],
        "additionalProperties": False,
        "properties": {"quote": {"type": "string", "minLength": 1},
                       "action": {"type": "string", "enum": ACTIONS},
                       "reason": {"type": "string"}}}}},
}

MAX_SUMMARY_CHARS = 450  # sufficient context for one-to-two-page applications

_CITED_TEXT = {"type": "object", "required": ["text", "basis"], "additionalProperties": False,
               "properties": {"text": {"type": "string", "minLength": 1},
                              "basis": BULLETS_SCHEMA["properties"]["bullets"]["items"]["properties"]["basis"]}}
# Two requests, not one: a rejected cover-letter quote must not cost the resume its summary.
SUMMARY_SCHEMA = {"type": "object", "required": ["summary"], "additionalProperties": False,
                  "properties": {"summary": _CITED_TEXT}}
COVER_LETTER_SCHEMA = {"type": "object", "required": ["cover_letter"], "additionalProperties": False,
                       "properties": {"cover_letter": {"type": "array", "minItems": 1, "maxItems": 5,
                                                       "items": _CITED_TEXT}}}


# ---------------------------------------------------------------- generation


class CVTailor:
    def __init__(self, llm, materials, by_project, resume_entry, timeout_seconds=None):
        self.llm = llm
        self.materials = materials
        self.by_id = {m["material_id"]: m for m in materials}
        self.by_project = by_project
        self.resume = resume_entry
        self.timeout = timeout_seconds
        self.warnings = []
        self.family = None   # job family of the JD (job_family.py); set by tailor()

    @staticmethod
    def _visible(entries):
        return visible_materials(entries)

    def _fits(self, task, payload, schema):
        return self.llm.estimate_context(task, SYSTEM_PROMPT, payload, schema) \
            <= self.llm.prompt_char_budget

    def project_bullets(self, project, requirements):
        boundaries = [model_text(e["text"]) for e in self.by_project[project] if is_boundary(e)]
        entries = [e for e in self.by_project[project] if not is_boundary(e) and model_text(e["text"])]
        allowed = [e["material_id"] for e in entries] + [RESUME_ID]
        payload = {
            "task_note": f"为项目“{project}”写最多 {MAX_BULLETS} 条针对本 JD 的英文简历要点；"
                         "每条通常 25–40 个英文单词、不超过 360 字符，交代主要行动、技术与结果；保留必要背景和限定；优先覆盖 verdict=direct/related 的要求。"
                         "有真实量化结果才用数字，保留 best/validation/test 和个人/团队归属等限定，不能制造指标；requirement_ids 列出对应 JD 要求。"
                         "证据边界、局限和归属说明只作为限定词写进相关要点（如 “(design deliverables only)”），"
                         "不要单独写一条只说明没做什么或无法归属的要点。"
                         "title 是这个项目在英文简历里的标题，全英文，格式“Project Name  |  Type”："
                         "resume_draft 里已有该项目的英文标题就沿用，否则把项目名意译成英文，不得出现中文。",
            "jd_requirements": requirements,
            "materials": self._visible(entries),
            "resume_draft": {"material_id": RESUME_ID, "text": self.resume["text"]},
        }
        if FAMILY_FOCUS.get(self.family):
            payload["task_note"] += FAMILY_FOCUS[self.family]
        if boundaries:
            payload["boundaries"] = boundaries
            payload["task_note"] += ("boundaries 列出不能写成成果的限制（只用来约束措辞和限定词，不能作为 basis 引用，"
                                     "也不要单独写成一条要点）。")
        task = f"cv_bullets:{project}"
        if not self._fits(task, payload, BULLETS_SCHEMA):
            payload.pop("resume_draft")
            allowed = allowed[:-1]
            self.warnings.append(f"项目“{project}”的材料加简历底稿超出提示词预算，本次未提供底稿")
            if not self._fits(task, payload, BULLETS_SCHEMA):
                raise LLMError(f"项目“{project}”的材料超出提示词预算，请调大 context_budget",
                               error_type="context_budget")
        checks = {"count": 0, "dropped": 0}

        def check(d):
            checks["count"] += 1
            d["title"] = shorten_title(d.get("title"))
            if checks["count"] > 1 and isinstance(d.get("bullets"), list):
                # One retry to reword; after that repair or drop the bullet, not the project.
                checks["dropped"] = repair_after_retry(d["bullets"])
                trim_quotes_to_verbatim(d["bullets"], self.by_id, allowed)
            return english_title_errors(d.get("title"), project) + validate_basis(
                d.get("bullets", []), self.by_id, allowed, require_personal=True, no_course_codes=True,
                english_only=True, max_chars=MAX_BULLET_CHARS, resume_text=True, prune_quotes=True,
                jd_text=" ".join(str(r.get("text", "")) for r in requirements))
        data = self.llm.generate_json(task, SYSTEM_PROMPT, payload, BULLETS_SCHEMA, self.timeout,
                                      validate=check)
        if checks["dropped"]:
            self.warnings.append(f"项目“{project}”有 {checks['dropped']} 条要点重试后仍含内部核对用语，已删去")
        return data

    def resume_review(self, requirements):
        payload = {
            "task_note": "逐条审阅简历底稿里的项目要点和技能行：对本 JD 该保留、上移、下移、精简还是删除；"
                         "quote 必须是底稿逐字原文（可以只引用一句的开头部分，但要连续）。只列需要动作或值得强调的条目。",
            "jd_requirements": requirements,
            "resume_draft": self.resume["text"],
        }
        return self.llm.generate_json(
            "cv_review", SYSTEM_PROMPT, payload, REVIEW_SCHEMA, self.timeout,
            validate=lambda d: validate_quotes_in(d.get("items", []), self.resume["text"]))

    _CITE_RULES = ("只能使用 materials、profile 和 resume_draft 里的事实；gaps 里的要求不要声称具备，"
                   "可以坦诚说明愿意学习。给出 basis：material_id 必须是 materials/profile/resume_draft "
                   "里的 id，quote 必须逐字摘自该材料的文本，并保持材料原文的语言：材料是中文，quote 就复制中文原句，"
                   "绝不要翻译成英文（正文用英文写，quote 不翻译）。每段 1-2 条 quote 即可。JD 的措辞（jd_text、jd_requirements、gaps 里的话）"
                   "可以在正文里呼应，但绝不能作为 quote；引用 JD 会被程序拒绝。")

    def _closing_request(self, task, note, schema, requirements, ordered_bullets, gap_list, jd_text):
        """Payload and allowed material ids shared by the summary and the cover letter."""
        profile_entries = [e for e in self.materials if e["kind"] == "profile"]
        allowed = [e["material_id"] for e in profile_entries] + [RESUME_ID] + [
            b["material_id"] for p in ordered_bullets for bl in p["bullets"] for b in bl["basis"]]
        allowed = list(dict.fromkeys(allowed))
        cited = [self.by_id[i] for i in allowed if i != RESUME_ID and self.by_id[i]["kind"] != "profile"]
        payload = {
            "task_note": note + self._CITE_RULES,
            "jd_text": jd_text,
            "jd_requirements": [{k: r[k] for k in ("requirement_id", "text", "importance", "verdict")}
                                for r in requirements],
            "gaps": [{"requirement_id": g["requirement_id"], "text": g["text"],
                      "kind": g["kind"], "reason": g["reason"][:160]} for g in gap_list],
            "tailored_bullets": [{"project": p["project"], "bullets": [b["text"] for b in p["bullets"]]}
                                 for p in ordered_bullets],
            "materials": self._visible(cited),
            "profile": self._visible(profile_entries),
            "resume_draft": {"material_id": RESUME_ID, "text": self.resume["text"]},
        }
        if not self._fits(task, payload, schema):
            payload["materials"] = []
            warning = "摘要/Cover Letter 请求超出预算，未附带项目材料全文，仅提供已生成要点"
            if warning not in self.warnings:
                self.warnings.append(warning)
            allowed = [i for i in allowed if i == RESUME_ID or self.by_id[i]["kind"] == "profile"]
            if not self._fits(task, payload, schema):
                raise LLMError("摘要/Cover Letter 请求超出提示词预算，请调大 context_budget",
                               error_type="context_budget")
        return payload, allowed

    def summary(self, requirements, ordered_bullets, gap_list, jd_text):
        task = "cv_summary"
        title = re.search(r"^Title:\s*(.+)$", jd_text or "", re.M)
        role = (f"目标岗位是 “{title.group(1).strip()}”（只用来决定侧重点：摘要以候选人身份开头，如 “Master of IT graduate with …”，"
                "不要复述岗位名称或公司名，不要写 “… role suited to …”）。") if title else ""
        payload, allowed = self._closing_request(
            task, f"写一段 2–3 句、约 40–65 个英文单词、不超过 {MAX_SUMMARY_CHARS} 个字符的英文简历摘要（summary），"
                  f"针对本岗位突出最相关的已核对经历。{role}第一句就点明材料支持、且与该岗位直接相关的能力（如支持岗可写调试与问题分析、"
                  "Microsoft 365/Windows、技术文档；数据岗写 SQL、建模、报表；材料没有的经历一律不写），再用 tailored_bullets 里最贴近的项目佐证；"
                  "岗位不是 AI/ML 方向时，不要罗列 machine learning、deep learning、NLP、computer vision 等方向清单。",
            SUMMARY_SCHEMA, requirements, ordered_bullets, gap_list, jd_text)
        return self.llm.generate_json(
            task, SYSTEM_PROMPT, payload, SUMMARY_SCHEMA, self.timeout,
            validate=lambda d: validate_basis([d.get("summary", {})], self.by_id, allowed, label="summary ",
                                              jd_text=jd_text, require_personal=True, no_course_codes=True,
                                              prune_quotes=True, max_chars=MAX_SUMMARY_CHARS,
                                              english_only=True, resume_text=True))

    def cover_letter(self, requirements, ordered_bullets, gap_list, jd_text):
        task = "cv_cover_letter"
        payload, allowed = self._closing_request(
            task, "写一封 3-4 段的英文 Cover Letter（cover_letter），每段给出 basis。",
            COVER_LETTER_SCHEMA, requirements, ordered_bullets, gap_list, jd_text)
        return self.llm.generate_json(
            task, SYSTEM_PROMPT, payload, COVER_LETTER_SCHEMA, self.timeout,
            validate=lambda d: validate_basis(d.get("cover_letter", []), self.by_id, allowed,
                                              label="cover_letter 第 {index} 段", jd_text=jd_text,
                                              require_personal=True, no_course_codes=True, prune_quotes=True,
                                              english_only=True, allow_jd_names=True))


def tailor(llm, meta, matches, jd_text, materials, by_project, resume_entry,
           timeout_seconds=None):
    """Run all generation steps; per-step failures are recorded, not fatal."""
    tailor_obj = CVTailor(llm, materials, by_project, resume_entry, timeout_seconds)
    tailor_obj.family = job_family(job_title(jd_text))
    requirements = requirement_summary(matches)
    order = rank_projects(matches, by_project)
    result = {
        "schema_version": "1.0",
        "prompt_version": TAILOR_PROMPT_VERSION,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "run_id": meta["run_id"],
        "project_order": order,
        "projects": [],
        "resume_review": [],
        "summary": None,
        "cover_letter": [],
        "gaps": gaps(matches),
        "errors": [],
        "warnings": [],
    }
    for item in tailored_projects(order, by_project):
        project = item["project"]
        try:
            data = tailor_obj.project_bullets(project, requirements)
        except LLMError as exc:
            result["errors"].append({"step": f"bullets:{project}", "error_type": exc.error_type,
                                     "message": str(exc)})
            continue
        result["projects"].append({"project": project, "title": data.get("title", "").strip(),
                                   "note": data.get("note", ""),
                                   "bullets": [_attach_sources(b, tailor_obj.by_id)
                                               for b in data.get("bullets", [])]})
    try:
        result["resume_review"] = tailor_obj.resume_review(requirements)["items"]
    except LLMError as exc:
        result["errors"].append({"step": "resume_review", "error_type": exc.error_type,
                                 "message": str(exc)})
    try:
        data = tailor_obj.summary(requirements, result["projects"], result["gaps"], jd_text)
        result["summary"] = _attach_sources(data["summary"], tailor_obj.by_id)
    except LLMError as exc:
        result["errors"].append({"step": "summary", "error_type": exc.error_type, "message": str(exc)})
    try:
        data = tailor_obj.cover_letter(requirements, result["projects"], result["gaps"], jd_text)
        result["cover_letter"] = [_attach_sources(p, tailor_obj.by_id) for p in data["cover_letter"]]
    except LLMError as exc:
        result["errors"].append({"step": "cover_letter", "error_type": exc.error_type, "message": str(exc)})
    result["warnings"] = tailor_obj.warnings
    result["status"] = "complete" if not result["errors"] else "partial"
    return result


def redo_closing(llm, result, matches, jd_text, materials, by_project, resume_entry, timeout_seconds=None):
    """Retry only what is missing at the end of an earlier result (the summary
    and/or the cover letter), keeping its bullets, review and gaps."""
    tailor_obj = CVTailor(llm, materials, by_project, resume_entry, timeout_seconds)
    tailor_obj.family = job_family(job_title(jd_text))
    requirements = requirement_summary(matches)
    result = {**result, "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
              "errors": [e for e in result.get("errors", []) if e["step"] not in ("summary", "cover_letter",
                                                                                  "summary_letter")]}
    if not result.get("summary"):
        try:
            data = tailor_obj.summary(requirements, result["projects"], result["gaps"], jd_text)
            result["summary"] = _attach_sources(data["summary"], tailor_obj.by_id)
        except LLMError as exc:
            result["errors"].append({"step": "summary", "error_type": exc.error_type, "message": str(exc)})
    if not result.get("cover_letter"):
        try:
            data = tailor_obj.cover_letter(requirements, result["projects"], result["gaps"], jd_text)
            result["cover_letter"] = [_attach_sources(p, tailor_obj.by_id) for p in data["cover_letter"]]
        except LLMError as exc:
            result["errors"].append({"step": "cover_letter", "error_type": exc.error_type, "message": str(exc)})
    result["warnings"] = list(dict.fromkeys(result.get("warnings", []) + tailor_obj.warnings))
    result["status"] = "complete" if not result["errors"] else "partial"
    return result


def stale_citations(suggestions, by_id):
    """Citations of a saved suggestions file that no longer hold in the current
    library. Material ids are positional (M001, M002, ... in file and section
    order), so after sections are added or removed an old id can point at a
    different text or at nothing; the stored quote tells the two apart."""
    items = [b for p in suggestions.get("projects", []) for b in p.get("bullets", [])]
    items += [suggestions["summary"]] if suggestions.get("summary") else []
    items += suggestions.get("cover_letter", [])
    problems = []
    for item in items:
        for basis in item.get("basis", []):
            material = by_id.get(basis.get("material_id"))
            if material is None or not quote_in(basis.get("quote", ""), material["text"]):
                problems.append(f"{basis.get('material_id')}：{basis.get('quote', '')[:40]!r}")
    return problems


def _attach_sources(item, by_id):
    basis = []
    for b in item.get("basis", []):
        m = by_id[b["material_id"]]
        basis.append({**b, "kind": m["kind"], "project": m["project"], "section": m["section"],
                      "source": m["source"], "line_start": m["line_start"],
                      "line_end": m["line_end"]})
    return {**item, "basis": basis}


# ---------------------------------------------------------------- output

VERDICT_LABELS = {"direct": "直接支持", "related": "相关但不足",
                  "insufficient": "证据不足", "unprocessed": "未处理"}
IMPORTANCE_LABELS = {"required": "必需", "preferred": "优先", "unspecified": "未说明"}
ACTION_LABELS = {"keep": "保留并强调", "move_up": "上移", "move_down": "下移",
                 "trim": "精简", "remove": "删除"}
KIND_LABELS = {"experience": "已核对经历", "unverified": "待核对经历（仅底稿）",
               "profile": "自述事实", "resume": "简历底稿（未核对）"}
UNVERIFIED_KINDS = ("unverified", "resume")


def _cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def _source_line(basis):
    parts = []
    for b in basis:
        label = KIND_LABELS.get(b["kind"], b["kind"])
        where = "简历底稿" if b["kind"] == "resume" else \
            f"{b['source']} › {b['section']}（行 {b['line_start']}-{b['line_end']}）"
        parts.append(f"[{label}] {where}：“{b['quote']}”")
    return "；".join(parts)


def write_markdown(result, meta, matches, resume_path, out_path):
    lines = ["# CV 定制建议", ""]
    lines.append(f"- 匹配运行：{result['run_id']}（JD：{meta['input']['jd_path']}）")
    lines.append(f"- 简历底稿：{resume_path}")
    gen = meta.get("models", {}).get("generator", {})
    lines.append(f"- 生成模型：{gen.get('name')}；定制提示词版本 {result['prompt_version']}；"
                 f"生成时间 {result['generated_at']}")
    lines.append(f"- 状态：{result['status']}" + (
        "" if not result["errors"] else "（部分步骤失败，见文末）"))
    lines.append("- 说明：以下所有内容都是对已有材料的改写，每条附来源与逐字引用。"
                 "标为“简历底稿（未核对）”的依据没有经过项目证据核对；"
                 "判断为证据不足的要求列在“不要硬写的缺口”，不要写进简历。")
    lines += ["", "## 1. 岗位要求对照", "",
              "| ID | 要求 | 优先级 | 判断 | 证据项目 |", "| --- | --- | --- | --- | --- |"]
    for r in requirement_summary(matches):
        lines.append(f"| {r['requirement_id']} | {_cell(r['text'])} | "
                     f"{IMPORTANCE_LABELS.get(r['importance'], r['importance'])} | "
                     f"{VERDICT_LABELS.get(r['verdict'], r['verdict'])} | "
                     f"{_cell('、'.join(r['evidence_projects']) or '—')} |")
    lines += ["", "## 2. 建议的项目顺序", ""]
    for i, p in enumerate(result["project_order"], 1):
        lines.append(f"{i}. {p['project']}（证据得分 {p['evidence_score']}；"
                     "得分 = 直接支持×3 + 相关×1，仅用于排序）")
    lines += ["", "## 3. 各项目要点改写建议", ""]
    if not result["projects"]:
        lines.append("（无：要点生成步骤失败或没有可用项目）")
    for p in result["projects"]:
        lines += [f"### {p['project']}", ""] + ([f"简历标题：{p['title']}", ""] if p.get("title") else [])
        if p.get("note"):
            lines += [f"说明：{p['note']}", ""]
        for b in p["bullets"]:
            req = "、".join(b.get("requirement_ids") or []) or "—"
            lines.append(f"- {b['text']}")
            lines.append(f"  - 对应要求：{req}")
            lines.append(f"  - 来源：{_source_line(b['basis'])}")
        lines.append("")
    lines += ["## 4. 简历摘要建议", ""]
    if result["summary"]:
        lines += [result["summary"]["text"], "",
                  f"来源：{_source_line(result['summary']['basis'])}", ""]
    else:
        lines += ["（未生成）", ""]
    lines += ["## 5. 底稿逐条审阅", ""]
    if result["resume_review"]:
        lines += ["| 动作 | 底稿原文 | 理由 |", "| --- | --- | --- |"]
        for item in result["resume_review"]:
            lines.append(f"| {ACTION_LABELS.get(item['action'], item['action'])} | "
                         f"{_cell(item['quote'])} | {_cell(item['reason'])} |")
    else:
        lines.append("（未生成）")
    lines += ["", "## 6. 不要硬写的缺口", ""]
    if not result["gaps"]:
        lines.append("（无）")
    for g in result["gaps"]:
        kind = {"insufficient": "证据不足", "partial": "仅部分覆盖", "unprocessed": "未处理"}[g["kind"]]
        lines.append(f"- {g['requirement_id']} {g['text']}（{IMPORTANCE_LABELS.get(g['importance'], g['importance'])}，{kind}）：{g['reason']}")
    lines += ["", "## 7. Cover Letter 草稿", ""]
    if result["cover_letter"]:
        for para in result["cover_letter"]:
            lines += [para["text"], "", f"来源：{_source_line(para['basis'])}", ""]
    else:
        lines += ["（未生成）", ""]
    lines += ["## 8. 人工复核清单", "",
              "- 逐条核对“来源”里的引用是否真的支持改写后的说法，尤其是形容词和程度副词。",
              "- 标为“简历底稿（未核对）”的内容，确认底稿本身属实后再使用。",
              "- 第 6 节的缺口不要写进简历；面试可坦诚说明学习计划。",
              "- 数字已由程序核对存在于材料中，但语境是否一致仍需人工确认。"]
    if result["warnings"]:
        lines += ["", "## 警告", ""] + [f"- {w}" for w in result["warnings"]]
    if result["errors"]:
        lines += ["", "## 失败步骤", ""] + [
            f"- {e['step']}（{e['error_type']}）：{e['message']}" for e in result["errors"]]
    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def unique_output(run_dir, stem):
    path = run_dir / f"{stem}.md"
    counter = 2
    while path.exists():
        path = run_dir / f"{stem}-{counter}.md"
        counter += 1
    return path


def output_number(path, stem):
    """cv_suggestions.md -> 1, cv_suggestions-3.md -> 3 (the unique_output scheme)."""
    rest = Path(path).stem[len(stem):]
    return int(rest[1:]) if rest.startswith("-") and rest[1:].isdigit() else 1


def latest_output(run_dir, stem, suffix=".md"):
    """Newest file written by unique_output for this stem, or None."""
    files = [p for p in Path(run_dir).glob(f"{stem}*{suffix}")
             if p.stem == stem or (p.stem.startswith(stem + "-") and p.stem[len(stem) + 1:].isdigit())]
    return max(files, key=lambda p: output_number(p, stem)) if files else None


# ---------------------------------------------------------------- CLI


def main(argv=None):
    parser = argparse.ArgumentParser(description="根据匹配结果定制简历内容（Markdown）")
    parser.add_argument("--run", required=True, type=Path, help="match_job 的运行目录")
    parser.add_argument("--resume", required=True, type=Path, help="简历底稿（.docx/.md/.txt）")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--data", type=Path, default=PROJECT_ROOT / "experiences")
    parser.add_argument("--json", action="store_true", help="stdout 只输出结果 JSON")
    parser.add_argument("--letter-only", action="store_true",
                        help="沿用最新一份 cv_suggestions 的要点，只重做缺失的 summary 和 Cover Letter")
    parser.add_argument("--redo-closing", action="store_true",
                        help="与 --letter-only 一起用：即使已有 summary 和 Cover Letter 也重写")
    args = parser.parse_args(argv)
    try:
        meta, matches, jd_text = load_run(args.run)
        resume_text = read_resume(args.resume)
        config = load_config(args.config)
        llm = build_llm(config)
    except (OSError, ValueError, LLMError, KeyError, zipfile.BadZipFile) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2  # nothing written; 1 is reserved for "partial, files written"
    materials, by_project, resume_entry = load_materials(args.data, resume_text)
    print(f"[tailor-cv] 材料 {len(materials)} 条，项目 {len(by_project)} 个，"
          f"要求 {len(matches)} 条；开始生成……", file=sys.stderr)
    if args.letter_only:
        previous = latest_output(args.run, "cv_suggestions", ".json")
        if previous is None:
            print(f"错误：{args.run} 里没有 cv_suggestions*.json；先完整运行一次", file=sys.stderr)
            return 2
        earlier = json.loads(previous.read_text(encoding="utf-8"))
        if args.redo_closing:
            earlier = {**earlier, "summary": None, "cover_letter": []}
        stale = stale_citations(earlier, {m["material_id"]: m for m in materials})
        if stale:
            print(f"错误：{previous.name} 里有 {len(stale)} 处引用与当前经历库对不上（例如 {stale[0]}）；"
                  "经历库在那之后改过，请去掉 --letter-only 重新生成", file=sys.stderr)
            return 2
        result = redo_closing(llm, earlier, matches, jd_text,
                              materials, by_project, resume_entry, config.get("timeout_seconds"))
    else:
        result = tailor(llm, meta, matches, jd_text, materials, by_project, resume_entry,
                        config.get("timeout_seconds"))
    result["resume_path"] = str(args.resume)
    out_md = unique_output(args.run, "cv_suggestions")
    out_json = out_md.with_suffix(".json")
    write_markdown(result, meta, matches, args.resume, out_md)
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps({**result, "output_md": str(out_md)}, ensure_ascii=False))
    else:
        print(f"已写入：{out_md}")
        print(f"状态：{result['status']}；要点 "
              f"{sum(len(p['bullets']) for p in result['projects'])} 条，缺口 {len(result['gaps'])} 条")
        for e in result["errors"]:
            print(f"失败步骤 {e['step']}：{e['message']}")
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
