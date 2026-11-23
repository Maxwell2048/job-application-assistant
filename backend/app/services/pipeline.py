"""Stage orchestration for one generation task.

Moved from webapp.Pipeline. The stages are the existing command-line tools
run as subprocesses (match_job, tailor_cv, build_resume, job_identity,
export_docx, interview_prep); no generation logic lives here. A Task holds
the live state of one run; every change is reported through `on_change` so
the worker can persist it and push it to the page.
"""
import json
import os
import re
import shutil
import queue
import threading
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from ..core import paths  # noqa: F401  (puts the engine on sys.path)
import add_jd
from local_llm import CALL_LOG_ENV, load_config
from .runs import condition_coverage

PYTHON = sys.executable

STAGES = [("save", "保存 JD"), ("match", "提取与匹配"), ("tailor", "CV 建议与 Cover Letter"),
          ("build", "组装简历"), ("docx", "导出 Word"), ("interview", "面试准备")]


def run_command(args, cwd, task=None):
    """Default stage runner: (returncode, stdout, stderr). The process is
    registered on the task so that a cancel request can stop it."""
    env = None
    if getattr(task, "llm_log", None):
        env = {**os.environ, CALL_LOG_ENV: str(task.llm_log)}   # one log line per model request
    process = subprocess.Popen([PYTHON, "-u", "-X", "utf8", *args], cwd=cwd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=env)
    if task is not None:
        task.process = process
    messages = queue.Queue()
    output = {"stdout": [], "stderr": []}

    def read_stream(name, stream):
        try:
            for line in stream:
                messages.put((name, line))
        finally:
            stream.close()
            messages.put((name, None))

    readers = [threading.Thread(target=read_stream, args=(name, stream), daemon=True)
               for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))]
    for reader in readers:
        reader.start()
    closed = 0
    try:
        while closed < 2:
            try:
                name, line = messages.get(timeout=1)
            except queue.Empty:
                line, name = "", None
            if name is not None:
                if line is None:
                    closed += 1
                    continue
                output[name].append(line)
            callback = getattr(task, "on_progress", None)
            if callback:
                callback(line.strip())
        process.wait()
    finally:
        for reader in readers:
            reader.join()
        if task is not None:
            task.process = None
    return process.returncode, "".join(output["stdout"]), "".join(output["stderr"])



def kill_tree(process):
    """Stop a stage and its children; on Windows the venv python.exe is a
    launcher whose real interpreter is a child process."""
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)
    else:
        process.kill()


def launch_with_shell(launcher):
    """Start the user's own model launcher in a new console window and return."""
    if os.name == "nt":
        subprocess.Popen(["cmd", "/c", "start", "", str(launcher)], cwd=str(Path(launcher).parent))
    else:
        subprocess.Popen([str(launcher)], cwd=str(Path(launcher).parent))


class LocationBlocked(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


REDO_STEPS = {"tailor": "tailor", "closing": "tailor", "assemble": "build", "export": "docx"}


class Task:
    """kind "full" runs every stage for a JD; kind "interview" adds interview
    prep to a run that already exists; kind "redo" repeats the later stages of
    a finished run from `redo` ("tailor", "closing" = summary and letter only,
    "assemble", "export") without matching the JD again."""

    def __init__(self, kind="full", jd_name="", with_interview=False, run_dir=None, job_id=None,
                 jd_path=None, redo=None, force=False):
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind
        self.jd_name = jd_name or ""
        self.with_interview = with_interview
        self.job_id = job_id
        self.created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.started_stamp = time.strftime("%Y%m%d-%H%M%S")  # same format as run directory names
        self.stages = [{"key": k, "label": l, "status": "pending", "seconds": None, "message": ""}
                       for k, l in STAGES]
        for stage in self.stages:
            is_interview = stage["key"] == "interview"
            if (kind == "interview" and not is_interview) or (kind == "full" and is_interview and not with_interview):
                stage["status"] = "skipped"
        if kind == "answer":   # one stage of its own: answering an application question
            self.stages = [{"key": "answer", "label": "回答申请问题", "status": "pending", "seconds": None,
                            "message": ""}]
        if kind == "redo":
            first = [k for k, _ in STAGES].index(REDO_STEPS[redo])
            for index, stage in enumerate(self.stages):
                if index < first or stage["key"] == "interview":
                    stage["status"] = "skipped"
        if kind == "full" and jd_path is not None:
            self.stage("save")["status"] = "skipped"  # the JD is already a file (inbox/SEEK)
        self.run_dir = Path(run_dir) if run_dir else None
        self.jd_path = Path(jd_path) if jd_path else None
        self.status = "queued"
        self.error = None
        self.process = None
        self.cancel_requested = False
        self.priority = 0      # set by the worker's queue: lower runs first
        self.attempt = 1       # a failed full generation is tried once more
        self.redo = redo
        self.force = force     # generate even when the evidence covers few of the job's conditions
        self.llm_log = None    # per-task model call log, copied into the run directory
        self.request = None    # kind "answer": {"id", "question", "max_words"}

    def stage(self, key):
        return next(s for s in self.stages if s["key"] == key)

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "jd_name": self.jd_name, "job_id": self.job_id,
                "created_at": self.created_at, "status": self.status, "error": self.error, "attempt": self.attempt,
                "redo": self.redo,
                "run_id": Path(self.run_dir).name if self.run_dir else None, "stages": self.stages}


class Pipeline:
    """Runs the stages of one task, synchronously, in the caller's thread.
    `runner` is injectable for tests; `on_change(task)` is called after every
    state change."""

    def __init__(self, settings, runner=run_command, on_change=None):
        self.root = Path(settings.project_root)
        self.jobs_dir = Path(settings.jobs_dir)
        self.outputs_dir = Path(settings.outputs_dir)
        self.resume_path = Path(settings.resume_path)
        self.config_path = Path(settings.config_path)
        self.gate = float(getattr(settings, "coverage_gate_max_gap_share", 0) or 0)
        self.calls_dir = self.outputs_dir / "_llm_calls"
        self.runner = runner
        self.on_change = on_change or (lambda task: None)

    # -- model server -----------------------------------------------------
    def model_online(self):
        try:
            config = load_config(self.config_path)
            base = config["base_url"].rstrip("/")
            url = base + ("/models" if base.endswith("/v1") else "/v1/models")
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {config.get('api_key', '')}"})
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status == 200, config.get("model")
        except Exception as exc:  # noqa: BLE001 - any failure means "offline" for the page
            return False, str(exc)

    def model_preflight(self):
        """One synthetic, source-validated extraction; no user run is created."""
        from local_llm import build_llm
        from jd_parser import extract_requirements
        try:
            config = load_config(self.config_path)
            config = {**config, "max_output_tokens": min(config["max_output_tokens"], 4096)}
            llm = build_llm(config)
            llm.max_attempts = 1
            result = extract_requirements("Requirements\nPython development is required.", llm, timeout_seconds=45)
            if result["failed_batches"] or not result["requirements"]:
                reason = result["failed_batches"][0]["message"] if result["failed_batches"] else "没有提取到测试要求"
                return False, "JD 提取预检失败：" + reason[:240]
            return True, config["model"]
        except Exception as exc:
            return False, "模型结构预检失败：" + str(exc)[:240]

    # -- helpers ------------------------------------------------------------
    def _step(self, task, key, func):
        stage = task.stage(key)
        stage["status"] = "running"
        self.on_change(task)
        started = time.time()
        last_update = 0.0
        last_output = started
        latest = "正在处理，请稍候"

        def progress(line):
            nonlocal last_update, last_output, latest
            now = time.time()
            if line.startswith("[progress] ") or line.startswith("[match-job] "):
                latest = line.split("] ", 1)[1][:240]
                last_output = now
            if now - last_update >= 2:
                stage["seconds"] = round(now - started, 1)
                quiet = int(now - last_output)
                stage["message"] = latest + (f" · 等待模型返回 {quiet}s" if quiet >= 10 else "")
                self.on_change(task)
                last_update = now

        task.on_progress = progress
        try:
            message = func()
            stage["status"] = "done"
            stage["message"] = message or ""
        except Exception as exc:  # noqa: BLE001 - reported on the page, not raised further than the task
            stage["status"] = "failed"
            stage["message"] = str(exc)
            raise
        finally:
            task.on_progress = None
            stage["seconds"] = round(time.time() - started, 1)
            self.on_change(task)

    def _cli(self, script, *args, ok_codes=(0,), task=None):
        if task is not None and task.cancel_requested:
            raise Cancelled()
        code, stdout, stderr = self.runner([str(self.root / script), *args], str(self.root), task=task)
        if task is not None and task.cancel_requested:
            raise Cancelled()
        if code not in ok_codes:
            tail = "\n".join((stderr or stdout).strip().splitlines()[-3:])
            raise RuntimeError(f"{script} 退出码 {code}：{tail}")
        return code, stdout, stderr

    @staticmethod
    def _partial_match_message(run_dir):
        """A partial match (some requirements unprocessed) continues: downstream
        tools list unprocessed items as gaps. A failed run, or one without
        matches, stops with the recorded errors rather than a stderr tail."""
        run_dir = Path(run_dir)
        try:
            meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        errors = "；".join(e.get("message", "")[:160] for e in meta.get("errors", [])[:3])
        if meta.get("run_status") != "partial" or not (run_dir / "matches.json").exists():
            raise RuntimeError(f"匹配失败（{meta.get('run_status', '状态未知')}）：{errors or '见 run_meta.json'}")
        matches = json.loads((run_dir / "matches.json").read_text(encoding="utf-8")).get("matches", [])
        unprocessed = [m["requirement_id"] for m in matches if m.get("processing_status") != "ok"]
        return (f"partial：{len(matches)} 条要求中 {len(unprocessed)} 条未处理"
                f"（{'、'.join(unprocessed[:6])}），已继续；详见匹配报告")

    # -- the two task kinds -------------------------------------------------
    def run_full(self, task, jd_text=None):
        task.status = "running"
        task.llm_log = self.calls_dir / f"{task.id}.jsonl"
        self.on_change(task)
        try:
            if task.jd_path is None:
                def save():
                    path = add_jd.save_jd(jd_text, self.jobs_dir, task.jd_name or None)
                    task.jd_path = path
                    return path.name
                self._step(task, "save", save)

            def match():
                # Exit 1 covers both "failed" and "partial"; only the run metadata tells
                # them apart, so accept it here and decide below.
                code, stdout, stderr = self._cli("match_job.py", "--jd", str(task.jd_path), "--config",
                                                 str(self.config_path), "--offline", "--out",
                                                 str(self.outputs_dir), ok_codes=(0, 1, 3), task=task)
                found = re.search(r"运行目录：(.+)", stdout)
                if not found:
                    tail = "\n".join((stderr or stdout).strip().splitlines()[-3:])
                    raise RuntimeError(f"match_job 未报告运行目录（退出码 {code}）：{tail}")
                task.run_dir = Path(found.group(1).strip())
                if code == 3:
                    screen = json.loads((task.run_dir / "location_screen.json").read_text(encoding="utf-8"))
                    raise LocationBlocked(screen["message"] + "\n地点依据：" + screen["location_quote"]
                                          + "\n出勤依据：" + screen["attendance_quote"])
                if code == 1:
                    return self._partial_match_message(task.run_dir)
                status = re.search(r"状态：(\S+)", stdout)
                return status.group(1) if status else ""
            self._step(task, "match", match)

            if not self._below_gate(task):
                self._later_stages(task, "tailor")
                if task.with_interview:
                    self._step(task, "interview", lambda: self._interview(task))
                task.status = "done"
        except Cancelled:
            self._finish_cancelled(task)
        except LocationBlocked as exc:
            task.status = "blocked"
            task.error = str(exc)
            task.stage("match")["status"] = "blocked"
            for stage in task.stages:
                if stage["status"] == "pending":
                    stage["status"] = "skipped"
        except Exception as exc:  # noqa: BLE001
            if task.cancel_requested:  # the killed stage surfaces as an ordinary failure
                self._finish_cancelled(task)
            else:
                task.status = "failed"
                task.error = str(exc)
        self._collect_calls(task)
        self.on_change(task)
        return task

    # -- stages shared by full and redo tasks ------------------------------------
    def _later_stages(self, task, start):
        """tailor -> build -> docx (with the quality report), from `start` on."""
        if start in ("tailor", "closing"):
            def tailor():
                extra = ["--letter-only", "--redo-closing"] if start == "closing" else []
                code, stdout, _ = self._cli("tailor_cv.py", "--run", str(task.run_dir), "--resume",
                                            str(self.resume_path), "--config", str(self.config_path), *extra,
                                            ok_codes=(0, 1), task=task)
                return "partial（部分步骤失败，见 CV 建议末尾）" if code == 1 else ""
            self._step(task, "tailor", tailor)
        if start in ("tailor", "closing", "assemble"):
            def build():
                self._cli("build_resume.py", "--run", str(task.run_dir), "--resume",
                          str(self.resume_path), "--config", str(self.config_path), task=task)
                return ""
            self._step(task, "build", build)

        def docx():
            # Company and job title for the file names; a failure only means plainer names.
            code, identity, _ = self._cli("job_identity.py", "--run", str(task.run_dir), "--config",
                                          str(self.config_path), ok_codes=(0, 1), task=task)
            label = ["--label", task.jd_name] if task.jd_name else []
            _, stdout, _ = self._cli("export_docx.py", "--run", str(task.run_dir), "--letter", *label,
                                     task=task)
            named = identity.strip().splitlines()[-1] if code == 0 and identity.strip() else "未识别出公司"
            return f"{stdout.count('已写入')} 个文件 · {named}" + self._quality(task)
        self._step(task, "docx", docx)

    def _quality(self, task):
        """Write quality.json for the run; ' · 质量：…' for the stage message, '' if it failed."""
        self._collect_calls(task)
        try:
            _, stdout, _ = self._cli("quality_report.py", "--run", str(task.run_dir), "--resume",
                                     str(self.resume_path), ok_codes=(0, 1, 2), task=task)
        except Cancelled:
            raise
        except Exception:  # noqa: BLE001 - a missing report must not fail a finished resume
            return ""
        found = re.search(r"质量：(.+)", stdout or "")
        return f" · 质量：{found.group(1).strip()}" if found else ""

    def _below_gate(self, task):
        """Stop an automatically queued job whose evidence covers too few of the
        job's conditions; the match report stays and generation can be resumed
        from the job page. True when the task stopped here."""
        if task.force or not self.gate or task.run_dir is None:
            return False
        coverage = condition_coverage(task.run_dir)
        total = coverage.get("total", 0)
        if total < 5:
            return False
        gaps = coverage.get("insufficient", 0) + coverage.get("error", 0)
        if gaps / total <= self.gate:
            return False
        task.status = "low_coverage"
        task.error = (f"证据覆盖不足：{total} 项入职条件中 {gaps} 项证据不足（超过 {self.gate:.0%}），"
                      "已停止生成，只保留匹配报告；可在岗位页继续生成")
        for stage in task.stages:
            if stage["status"] == "pending":
                stage["status"], stage["message"] = "skipped", "证据覆盖不足，未生成"
        return True

    def _collect_calls(self, task):
        """Move the task's model call log into the run directory (appending)."""
        log = getattr(task, "llm_log", None)
        if not log or task.run_dir is None or not Path(log).is_file() or not Path(task.run_dir).is_dir():
            return
        try:
            with open(Path(task.run_dir) / "llm_calls.jsonl", "a", encoding="utf-8") as target:
                target.write(Path(log).read_text(encoding="utf-8"))
            Path(log).unlink()
        except OSError:
            pass

    def run_redo(self, task):
        """Repeat the later stages of an existing run (see Task)."""
        task.status = "running"
        task.llm_log = self.calls_dir / f"{task.id}.jsonl"
        self.on_change(task)
        try:
            self._later_stages(task, task.redo)
            task.status = "done"
        except Exception as exc:  # noqa: BLE001
            if task.cancel_requested:
                task.status, task.error = "cancelled", "已中止；已生成的文件保留"
                for stage in task.stages:
                    if stage["status"] == "running":
                        stage["status"], stage["message"] = "cancelled", "已中止"
                    elif stage["status"] == "pending":
                        stage["status"] = "skipped"
            else:
                task.status = "failed"
                task.error = str(exc)
        self._collect_calls(task)
        self.on_change(task)
        return task

    def run_answer(self, task):
        """Answer one application-form question from the run's verified material."""
        task.status = "running"
        task.llm_log = self.calls_dir / f"{task.id}.jsonl"
        self.on_change(task)
        request = Path(task.run_dir) / f"answer_request-{task.id}.json"
        try:
            request.write_text(json.dumps(task.request, ensure_ascii=False), encoding="utf-8")

            def answer():
                code, stdout, stderr = self._cli("application_answer.py", "--run", str(task.run_dir), "--resume",
                                                 str(self.resume_path), "--config", str(self.config_path),
                                                 "--request", str(request), ok_codes=(0, 1), task=task)
                if code == 1:
                    raise RuntimeError(((stderr or stdout).strip().splitlines() or ["回答生成失败"])[-1][:300])
                return stdout.strip().splitlines()[-1] if stdout.strip() else ""
            self._step(task, "answer", answer)
            task.status = "done"
        except Exception as exc:  # noqa: BLE001
            task.status = "cancelled" if task.cancel_requested else "failed"
            task.error = "已中止" if task.cancel_requested else str(exc)
        finally:
            request.unlink(missing_ok=True)
        self._collect_calls(task)
        self.on_change(task)
        return task

    def _interview(self, task):
        code, _, _ = self._cli("interview_prep.py", "--run", str(task.run_dir), "--config",
                               str(self.config_path), ok_codes=(0, 1), task=task)
        return "partial" if code == 1 else ""

    def run_interview(self, task):
        task.status = "running"
        self.on_change(task)
        try:
            self._step(task, "interview", lambda: self._interview(task))
            task.status = "done"
        except Exception as exc:  # noqa: BLE001
            if task.cancel_requested:
                self._finish_cancelled(task)
            else:
                task.status = "failed"
                task.error = str(exc)
        self.on_change(task)
        return task

    # -- cancellation -------------------------------------------------------
    def cancel(self, task):
        """Stop the running stage; the running thread then removes what it created."""
        if task.status not in ("queued", "running"):
            return False
        task.cancel_requested = True
        kill_tree(task.process)
        if task.status == "queued":  # never started: finish it here
            self._finish_cancelled(task)
            self.on_change(task)
        return True

    def _run_dir_for(self, task):
        """The run directory match_job created for this task, when the stage was
        stopped before it reported one. run_meta.json is only written at the
        end, so the run is recognised by its copy of the JD, and only among
        directories made after the task began: an earlier, finished run of the
        same JD must never be taken for the unfinished one."""
        if task.jd_path is None or not self.outputs_dir.is_dir():
            return None
        try:
            jd_text = Path(task.jd_path).read_text(encoding="utf-8")
        except OSError:
            return None
        for run in sorted(self.outputs_dir.iterdir(), reverse=True):
            if not run.is_dir() or run.name[:15] < task.started_stamp:
                continue
            try:
                if (run / "jd.txt").read_text(encoding="utf-8") == jd_text:
                    return run
            except OSError:
                continue
        return None

    def _discard_unfinished(self, task):
        """A cancelled full task leaves nothing behind: its run directory and,
        when the JD was pasted for this task, the JD file it saved. JD files
        that arrived from a source (inbox/SEEK) are kept. A cancelled interview
        task only stops; the finished run it was adding to is kept."""
        removed = []
        if task.kind != "full":
            return removed
        outputs = self.outputs_dir.resolve()
        if task.run_dir and Path(task.run_dir).resolve().parent == outputs and Path(task.run_dir).is_dir():
            shutil.rmtree(task.run_dir, ignore_errors=True)
            removed.append(Path(task.run_dir).name)
        if task.stage("save")["status"] == "done" and task.jd_path \
                and Path(task.jd_path).resolve().parent == self.jobs_dir.resolve() and Path(task.jd_path).is_file():
            Path(task.jd_path).unlink()
            removed.append(Path(task.jd_path).name)
        return removed

    def _finish_cancelled(self, task):
        if task.kind == "full" and task.run_dir is None:
            task.run_dir = self._run_dir_for(task)
        removed = self._discard_unfinished(task)
        if task.kind == "full":
            task.run_dir = None
        task.status = "cancelled"
        task.error = "已中止" + (f"，已删除未完成的内容：{'、'.join(removed)}" if removed else "")
        for stage in task.stages:
            if stage["status"] in ("running", "failed"):
                stage["status"], stage["message"] = "cancelled", "已中止"
            elif stage["status"] == "pending":
                stage["status"] = "skipped"
