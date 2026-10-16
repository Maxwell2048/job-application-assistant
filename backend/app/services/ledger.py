"""Ingest collected jobs into the ledger: dedupe by (source, external id),
notice changed JDs, recognise reposts, keep the JD text on disk."""
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

from sqlalchemy import select

from ..core import paths  # noqa: F401
import add_jd
from ..db.models import Job

_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def jd_file_for(jobs_dir, source, external_id):
    return Path(jobs_dir) / f"{source}-{_SAFE.sub('-', str(external_id)).strip('-')[:80]}.txt"


def _norm_title(text):
    return re.sub(r"\W+", " ", text or "").strip().casefold()


def ingest(session, item, jobs_dir):
    """item: {source, external_id, title, company, url, location, work_type,
    listed_at, jd_text, expired}. Returns (job, outcome) with outcome one of
    new | seen | updated | repost | no_jd."""
    now = datetime.now(timezone.utc)
    jd_text = add_jd.normalise(item.get("jd_text") or "") if item.get("jd_text") else ""
    digest = hashlib.sha256(jd_text.encode("utf-8")).hexdigest() if jd_text else ""
    job = session.scalar(select(Job).where(Job.source == item["source"], Job.external_id == str(item["external_id"])))
    outcome = "seen"
    if job is None:
        job = Job(source=item["source"], external_id=str(item["external_id"]), first_seen=now,
                  status="new" if jd_text else "no_jd")
        session.add(job)
        outcome = "new" if jd_text else "no_jd"
    job.last_seen = now
    for key in ("title", "company", "url", "location", "work_type", "listed_at"):
        if item.get(key):
            setattr(job, key, str(item[key])[:300])
    if item.get("expired") and job.status in ("new", "no_jd", "held", "screened_out"):
        job.status = "expired"
    if jd_text and digest != job.jd_sha256:
        if job.jd_sha256:                    # the employer edited the ad after we saw it
            outcome = "updated"
            job.note = (job.note + "\n" if job.note else "") + f"JD 内容于 {now:%Y-%m-%d %H:%M} 更新"
        elif outcome == "seen":              # first time we have the full text of a known listing
            outcome = "new"
            if "手动跳过" not in (job.note or "") and "搜索入口排除" not in (job.note or ""):
                job.status = "new"
        job.jd_sha256 = digest
        path = jd_file_for(jobs_dir, job.source, job.external_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(jd_text, encoding="utf-8")
        job.jd_path = str(path)
    if outcome == "new" and jd_text:
        twin = session.scalar(select(Job).where(Job.jd_sha256 == digest, Job.id != job.id,
                                                Job.repost_of.is_(None)).order_by(Job.first_seen))
        if twin is not None and twin.external_id != job.external_id:
            job.repost_of = twin.id
            job.status = "screened_out"
            job.note = (job.note + "\n" if job.note else "") + f"与 #{twin.id}（{twin.company} / {twin.title}）JD 相同，视为重复发布"
            outcome = "repost"
        else:
            same_ad = session.scalar(select(Job).where(Job.id != job.id, Job.company == job.company,
                                                       Job.title == job.title, Job.jd_sha256 != ""))
            if same_ad is not None and _norm_title(same_ad.title) == _norm_title(job.title) and job.company:
                job.note = (job.note + "\n" if job.note else "") + f"公司和标题与 #{same_ad.id} 相同，但 JD 不同；可能是重发"
    session.flush()
    return job, outcome


def relink_jd_files(session_factory, jobs_dir):
    """JD paths are stored whole; after the project folder moves (D: to E:, say)
    they point nowhere. Re-point each missing one to the file of the same name
    in jobs_dir when it is there. -> the number re-linked."""
    fixed = 0
    with session_factory() as session:
        for job in session.scalars(select(Job).where(Job.jd_path != "")).all():
            if Path(job.jd_path).is_file():
                continue
            candidate = Path(jobs_dir) / PureWindowsPath(job.jd_path).name   # either separator
            if candidate.is_file():
                job.jd_path = str(candidate)
                fixed += 1
        session.commit()
    return fixed
