"""The tick: make sure the model is up, collect, ingest, screen, queue.

Runs every `autopilot_interval_hours` from a thread inside the backend, on
request from the API, or from the command line (python -m backend.app.autopilot).
One tick at a time; a tick never generates anything itself, it only hands
eligible jobs to the worker's queue.
"""
import json
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from ..core import paths  # noqa: F401
from local_llm import build_llm, load_config
from ..db.models import Job, Screening, Task as TaskRow
from . import screen as screening
from .ledger import ingest
from .location_rules import employer_body
from .pipeline import launch_with_shell
from .worker import PRIORITY_SCREENED
from tools.seek_collector.search_rules import IT_TITLE
from .sources import inbox as inbox_source
from .sources import seek as seek_source

RESCREEN_STATUSES = ("new", "held")
# what `rescreen` looks at by default: everything already decided that has a JD
RESCREEN_EXISTING = ("done", "failed", "cancelled", "screened_out", "blocked", "ready")


def screening_failed(row):
    """Technical failures, including legacy rows written before status markers."""
    if row is None:
        return False
    rules = row.rule_result or {}
    rights = rules.get("work_rights") or {}
    fit = rules.get("fit") or {}
    return (rights.get("processing_status") == "error"
            or fit.get("processing_status") == "error"
            or "模型判断失败" in rights.get("reason", "")
            or any("匹配度预筛失败" in flag for flag in fit.get("flags", [])))


class Autopilot:
    def __init__(self, settings, session_factory, worker, launch=launch_with_shell, seek_runner=None,
                 llm_factory=None):
        self.settings = settings
        self.session_factory = session_factory
        self.worker = worker
        self.launch = launch
        self.seek_runner = seek_runner
        self.llm_factory = llm_factory or (lambda: build_llm(load_config(settings.config_path)))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.running = False
        self.last_report = None
        self.next_run_at = None
        prefs = self._load_prefs()
        mode = prefs.get("mode")
        self.mode = mode if mode in seek_source.MODES else settings.search_mode
        generate = prefs.get("auto_generate")
        self.auto_generate = generate if isinstance(generate, bool) else settings.auto_generate

    # -- choices made on the page (search mode, generate or only list) ------------
    def _mode_file(self):
        return Path(self.settings.project_root) / "state" / "autopilot_mode.json"

    def _load_prefs(self):
        """What was chosen on the page, which outlives restarts; {} when nothing was."""
        try:
            prefs = json.loads(self._mode_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return prefs if isinstance(prefs, dict) else {}

    def _save_prefs(self):
        path = self._mode_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mode": self.mode, "auto_generate": self.auto_generate}), encoding="utf-8")

    def set_mode(self, mode):
        if mode not in seek_source.MODES:
            raise ValueError(f"未知的搜索模式：{mode}")
        self.mode = mode
        self._save_prefs()
        return mode

    def set_auto_generate(self, enabled):
        """True: eligible jobs are queued for generation; False: they wait as "ready"."""
        self.auto_generate = bool(enabled)
        self._save_prefs()
        return self.auto_generate

    # -- scheduling ------------------------------------------------------------
    def start(self):
        if self.settings.autopilot_enabled and self._thread is None:
            self.next_run_at = datetime.now(timezone.utc) + timedelta(minutes=1)  # first tick soon after start
            self._thread = threading.Thread(target=self._loop, name="autopilot", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(timeout=15):
            if self.next_run_at and datetime.now(timezone.utc) >= self.next_run_at:
                self.tick(trigger="schedule")

    def status(self):
        return {"enabled": self.settings.autopilot_enabled, "running": self.running,
                "interval_hours": self.settings.autopilot_interval_hours,
                "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
                "last_report": self.last_report,
                "collector_configured": bool(self.settings.collector_dir),
                "model_launcher_configured": bool(self.settings.model_launcher),
                "mode": self.mode, "auto_generate": self.auto_generate,
                "mode_limits": {m: dict(zip(("max_pages", "max_details"), seek_source.mode_limits(self.settings, m)))
                                for m in seek_source.MODES}}

    # -- model ------------------------------------------------------------------
    def ensure_model(self, report):
        online, info = self.worker.model_online()
        if online:
            report["model_online"] = True
            return True
        if not self.settings.model_launcher:
            report["model_online"] = False
            report["notes"].append(f"模型离线且未配置启动脚本：{info}")
            return False
        report["notes"].append("模型离线，正在启动")
        self.launch(self.settings.model_launcher)
        deadline = time.time() + self.settings.model_start_timeout_seconds
        while time.time() < deadline and not self._stop.is_set():
            time.sleep(5)
            online, _ = self.worker.model_online()
            if online:
                report["model_online"] = True
                report["notes"].append(f"模型已启动（{int(time.time() - (deadline - self.settings.model_start_timeout_seconds))} 秒）")
                return True
        report["model_online"] = False
        report["notes"].append("模型启动超时")
        return False

    # -- the tick -----------------------------------------------------------------
    def tick(self, trigger="manual", max_pages=None, max_details=None, sources=("inbox", "seek"), mode=None,
             generate=None):
        """generate: queue eligible jobs for generation (True) or leave them "ready" for the
        user to pick (False); None uses the choice saved on the page."""
        mode = mode or self.mode
        generate = self.auto_generate if generate is None else bool(generate)
        if mode not in seek_source.MODES:
            raise ValueError(f"未知的搜索模式：{mode}")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("上一轮自动处理仍在进行")
        self.running = True
        started = time.time()
        report = {"id": uuid.uuid4().hex[:8], "trigger": trigger, "mode": mode, "generate": generate,
                  "started_at": datetime.now(timezone.utc).isoformat(),
                  "model_online": None, "sources": {}, "collected": 0, "new": 0, "updated": 0, "seen": 0,
                  "repost": 0, "no_jd": 0, "queued": [], "flagged": [], "ready": [], "screened_out": [], "held": [],
                  "notes": [], "errors": []}
        self.worker.events.publish("autopilot", {"phase": "start", "report": report})
        try:
            model_online = self.ensure_model(report)
            items = self._collect(report, sources, max_pages, max_details, mode)
            with self.session_factory() as session:
                for item in items:
                    try:
                        job, outcome = ingest(session, item, self.settings.jobs_dir)
                        report[outcome] = report.get(outcome, 0) + 1
                        if outcome == "repost":
                            report["screened_out"].append(self._brief(job, "重复发布", ""))
                    except Exception as exc:  # noqa: BLE001 - one bad item must not stop the tick
                        session.rollback()
                        report["errors"].append(f"入账 {item.get('source')}:{item.get('external_id')}：{exc}")
                session.commit()
            self._screen_pending(report, model_online, generate)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"{exc}")
            report["traceback"] = traceback.format_exc()
        finally:
            report["seconds"] = round(time.time() - started, 1)
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            self.last_report = report
            self._record(report)
            if self.settings.autopilot_enabled:
                self.next_run_at = datetime.now(timezone.utc) + timedelta(hours=self.settings.autopilot_interval_hours)
            self.running = False
            self._lock.release()
            self.worker.events.publish("autopilot", {"phase": "done", "report": report})
        return report

    def _collect(self, report, sources, max_pages, max_details, mode):
        items = []
        if "inbox" in sources:
            found = inbox_source.collect(self.settings)
            report["sources"]["inbox"] = {"items": len(found)}
            items += found
        if "seek" in sources and self.settings.collector_dir:
            try:
                kwargs = {"runner": self.seek_runner} if self.seek_runner else {}
                with self.session_factory() as session:   # jobs whose JD we hold: no need to read them again
                    known_ids = session.scalars(select(Job.external_id).where(Job.source == "seek",
                                                                              Job.jd_sha256 != "")).all()
                    pending_ids = self._pending_ids(session)
                found, info = seek_source.collect(self.settings, max_pages, max_details, mode=mode,
                                                  known_ids=known_ids, pending_ids=pending_ids, **kwargs)
                report["sources"]["seek"] = {"items": len(found), **info}
                if info.get("pending_requested"):
                    report["notes"].append(f"先读取只有标题的岗位详情：{info.get('pending_read', 0)} / "
                                           f"{info['pending_requested']} 个")
                if info.get("rate_limited"):
                    report["notes"].append("SEEK 限流（HTTP 429/403），本轮采集提前结束；没有完整 JD 的结果不入账")
                items += found
            except Exception as exc:  # noqa: BLE001 - the inbox still gets processed
                report["sources"]["seek"] = {"error": str(exc)}
                report["errors"].append(f"SEEK 采集：{exc}")
        items = [item for item in items if employer_body(item.get("jd_text", ""))]
        report["collected"] = len(items)
        return items

    def _judge(self, job, jd_text, model_online, llm_holder, known):
        """Listing rules, then the work-rights gate, then the fit gate, for one
        job. Writes nothing. Returns None when the rules pass but the model is
        offline; otherwise {rules, gate, fit, decision, reason, quote, label, fit_text}."""
        item = {"title": job.title, "location": job.location, "work_type": job.work_type, "jd_text": jd_text,
                "expired": False}
        rules = screening.rule_screen(item, self.settings)
        if rules["exclude"]:
            return {"rules": rules, "gate": None, "fit": None, "decision": "skip",
                    "reason": "；".join(rules["reasons"]), "quote": "", "label": "", "fit_text": ""}
        if not model_online:
            return None
        if llm_holder[0] is None:
            llm_holder[0] = self.llm_factory()
        llm = llm_holder[0]
        gate = screening.work_rights_gate(jd_text, llm)
        label = "工作权利筛选失败" if gate.get("processing_status") == "error" else screening.VERDICT_LABELS.get(gate["verdict"], gate["verdict"])
        fit = None
        if gate["decision"] != "skip" and self.settings.fit_screen_enabled:
            fit = screening.fit_gate(jd_text, job.title, llm, self.settings, known)
        decision = gate["decision"]
        if fit is not None and fit["decision"] == "skip":
            decision = "skip"
        elif fit is not None and fit["decision"] == "run_flagged" and decision == "run":
            decision = "run_flagged"
        fit_text = screening.fit_label(fit)
        if gate["decision"] == "skip":
            reason, quote = label, gate["quote"]
        elif decision == "skip":
            reason, quote = "；".join(fit["skips"]), fit_text
        else:
            reason, quote = " · ".join(p for p in (label, fit_text) if p), gate["quote"]
        return {"rules": rules, "gate": gate, "fit": fit, "decision": decision, "reason": reason, "quote": quote,
                "label": label, "fit_text": fit_text}

    @staticmethod
    def _screening_row(job, verdict):
        # The fit result travels inside rule_result: a new column would need a migration,
        # and existing SQLite ledgers created before Alembic cannot take one safely.
        rules, gate, fit = verdict["rules"], verdict["gate"], verdict["fit"]
        return Screening(job_id=job.id, rule_result={**rules, "work_rights": gate, **({"fit": fit} if fit else {})},
                         model_verdict=gate["verdict"] if gate else "", quote=gate["quote"] if gate else "",
                         decision=verdict["decision"],
                         prompt_version=gate["prompt_version"] if gate else screening.SCREEN_PROMPT_VERSION)

    def _known_stack(self, report, any_jobs):
        known = screening.known_stack(self.settings.profile_path) if self.settings.fit_screen_enabled else []
        if self.settings.fit_screen_enabled and any_jobs and not known:
            report["notes"].append(f"未在 {self.settings.profile_path} 找到“相关能力”清单，预筛不比对技术栈")
        return known

    @staticmethod
    def _pending_ids(session):
        """SEEK jobs known only by title, the ones worth a request first: junior or
        graduate titles, then the most recently seen. A job skipped by hand is
        screened_out, not no_jd, so it never spends a request."""
        jobs = session.scalars(select(Job).where(Job.source == "seek", Job.status == "no_jd", Job.jd_sha256 == "")
                               .order_by(Job.last_seen.desc(), Job.id.desc())).all()
        wanted = [j for j in jobs if screening.junior_by_rule(j.title, "")]
        ordered = wanted + [j for j in jobs if j not in wanted]
        ordered.sort(key=lambda j: not bool(IT_TITLE.search(j.title)))
        return [j.external_id for j in ordered]

    def _screen_pending(self, report, model_online, generate=True):
        """One short transaction per job. The model call happens with no write
        pending, so the worker thread's own writes are never blocked behind a
        long screening loop. Without `generate`, eligible jobs become "ready"
        instead of being queued."""
        with self.session_factory() as session:
            pending_ids = session.scalars(select(Job.id).where(Job.status.in_(RESCREEN_STATUSES))
                                          .order_by(Job.first_seen)).all()
        llm_holder = [None]
        eligible = []
        known = self._known_stack(report, pending_ids)
        for job_id in pending_ids:
            with self.session_factory() as session:
                job = session.get(Job, job_id)
                jd_text = self._jd_text(job)
                if not jd_text:
                    job.status = "no_jd"
                    session.commit()
                    continue
                verdict = self._judge(job, jd_text, model_online, llm_holder, known)
                if verdict is None:
                    job.status = "held"
                    report["held"].append(self._brief(job, "模型离线，等待下一轮筛选", ""))
                    session.commit()
                    continue
                session.add(self._screening_row(job, verdict))
                if verdict["decision"] == "skip":
                    job.status = "screened_out"
                    report["screened_out"].append(self._brief(job, verdict["reason"], verdict["quote"]))
                    session.commit()
                    continue
                gate, fit = verdict["gate"], verdict["fit"]
                flags = list(verdict["rules"]["flags"])
                if gate["decision"] == "run_flagged":
                    flags.insert(0, f"⚠ {verdict['label']}：{gate['reason']}")
                if fit is not None and fit["flags"]:
                    flags.append("预筛提示：" + "；".join(fit["flags"]))
                if flags:
                    job.note = (job.note + "\n" if job.note else "") + "\n".join(flags)
                brief = self._brief(job, verdict["reason"], verdict["quote"])
                priority = PRIORITY_SCREENED + screening.fit_rank(fit, self.settings)
                if not generate:
                    job.status = "ready"
                    report["ready"].append({**brief, "rank": priority, "decision": verdict["decision"]})
                    session.commit()
                    continue
                job.status = "queued"
                eligible.append((priority, job_id, job.company or job.title, Path(job.jd_path), verdict["decision"],
                                 brief))
                session.commit()
        report["ready"].sort(key=lambda b: (b["rank"], b["job_id"]))
        # Committed: the worker may now write to these job rows freely. Queued best first, so a
        # long batch that is stopped half-way has already produced the most promising jobs.
        for priority, job_id, jd_name, jd_path, decision, brief in sorted(eligible, key=lambda e: (e[0], e[1])):
            self.worker.submit_full(jd_name=jd_name, job_id=job_id, jd_path=jd_path, priority=priority)
            (report["flagged"] if decision == "run_flagged" else report["queued"]).append({**brief, "rank": priority})

    def rescreen(self, statuses=RESCREEN_EXISTING, requeue_failed=False, only_failed=False):
        """Screen again the jobs the ledger already holds, with today's rules and
        fit gate, without collecting anything. Each gets a new screening record;
        no status changes, except that with requeue_failed a failed job that
        passes goes back into the queue. A job someone skipped by hand stays
        skipped. only_failed repairs technical failures; pending ready jobs
        rejected by a successful new screen are removed from the ready queue.
        -> a report shaped like a tick's."""
        report = {"id": uuid.uuid4().hex[:8], "trigger": "rescreen_failed" if only_failed else "rescreen", "mode": "",
                  "started_at": datetime.now(timezone.utc).isoformat(), "model_online": None, "sources": {},
                  "collected": 0, "new": 0, "updated": 0, "seen": 0, "repost": 0, "no_jd": 0, "queued": [],
                  "flagged": [], "ready": [], "screened_out": [], "held": [], "notes": [], "errors": [],
                  "rescreened": []}
        started = time.time()
        model_online = self.ensure_model(report)
        with self.session_factory() as session:
            job_ids = session.scalars(select(Job.id).where(Job.status.in_(tuple(statuses)))
                                      .order_by(Job.first_seen)).all()
        llm_holder = [None]
        requeue = []
        known = self._known_stack(report, job_ids)
        for job_id in job_ids:
            with self.session_factory() as session:
                job = session.get(Job, job_id)
                jd_text = self._jd_text(job)
                if not jd_text:
                    report["no_jd"] += 1
                    continue
                if "手动跳过" in (job.note or "") or (only_failed and job.repost_of is not None):
                    continue
                if only_failed:
                    latest = max(job.screenings, key=lambda row: (row.created_at, row.id or 0), default=None)
                    if not screening_failed(latest):
                        continue
                try:
                    verdict = self._judge(job, jd_text, model_online, llm_holder, known)
                except Exception as exc:  # noqa: BLE001 - one job must not stop the rest
                    report["errors"].append(f"#{job.id} {job.title}：{exc}")
                    continue
                if verdict is None:
                    report["held"].append(self._brief(job, "模型离线，未补筛", ""))
                    continue
                row = self._screening_row(job, verdict)
                session.add(row)
                if only_failed and not screening_failed(row) and job.status == "ready" and verdict["decision"] == "skip":
                    job.status = "screened_out"
                fit = verdict["fit"]
                entry = {**self._brief(job, verdict["reason"], verdict["quote"]), "status": job.status,
                         "decision": verdict["decision"], "rank": PRIORITY_SCREENED + screening.fit_rank(fit, self.settings),
                         "flags": (fit or {}).get("flags", [])}
                report["rescreened"].append(entry)
                if requeue_failed and job.status == "failed" and verdict["decision"] != "skip" and job.jd_path:
                    job.status = "queued"
                    requeue.append((entry["rank"], job.id, job.company or job.title, Path(job.jd_path), entry))
                session.commit()
                if only_failed:
                    self.worker.events.publish("jobs", {"id": job.id, "status": job.status})
                    if screening_failed(row):
                        report["errors"].append(f"#{job.id} 筛选仍失败：{(verdict['gate'] or {}).get('reason', '')}；{';'.join((fit or {}).get('flags', []))}")
        for rank, job_id, jd_name, jd_path, entry in sorted(requeue, key=lambda e: (e[0], e[1])):
            self.worker.submit_full(jd_name=jd_name, job_id=job_id, jd_path=jd_path, priority=rank)
            report["queued"].append(entry)
        report["seconds"] = round(time.time() - started, 1)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        if only_failed:
            self.last_report = report
            self._record(report)
        return report

    @staticmethod
    def _jd_text(job):
        try:
            return Path(job.jd_path).read_text(encoding="utf-8") if job.jd_path else ""
        except OSError:
            return ""

    @staticmethod
    def _brief(job, reason, quote):
        return {"job_id": job.id, "title": job.title, "company": job.company, "url": job.url, "reason": reason,
                "quote": quote}

    def _record(self, report):
        """Keep the tick in the tasks table so the page can show a history."""
        try:
            with self.session_factory() as session:
                row = TaskRow(id=report["id"], kind="tick", status="failed" if report["errors"] else "done",
                              stages=[report], error="；".join(report["errors"])[:2000])
                row.started_at = datetime.fromisoformat(report["started_at"])
                row.finished_at = datetime.now(timezone.utc)
                session.add(row)
                session.commit()
        except Exception as exc:  # noqa: BLE001
            report["notes"].append(f"记录本轮失败：{exc}")
