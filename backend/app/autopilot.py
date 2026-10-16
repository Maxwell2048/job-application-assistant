"""Command-line entry for the autopilot, for headless use and debugging.

    python -m backend.app.autopilot tick                    # one round: inbox + SEEK, current mode
    python -m backend.app.autopilot tick --mode broad       # every ICT job in Perth, this round only
    python -m backend.app.autopilot tick --no-generate      # collect and screen only; eligible jobs wait as "ready"
    python -m backend.app.autopilot backfill --max-pages 3 --max-details 60
    python -m backend.app.autopilot mode broad              # switch the saved mode (focused | broad)
    python -m backend.app.autopilot rescreen --requeue-failed   # screen the ledger's jobs again, no collecting
    python -m backend.app.autopilot status

The generation itself happens in the worker of whichever process runs the
tick, so this waits for the queue to drain before exiting.
"""
import argparse
import json
import sys
import time

from .core.config import Settings
from .db.session import create_schema, make_engine, make_session_factory
from .services.autopilot import RESCREEN_EXISTING, Autopilot
from .services.ledger import relink_jd_files
from .services.worker import Worker


def main(argv=None):
    parser = argparse.ArgumentParser(description="求职 RAG 自动处理")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("tick", "backfill"):
        p = sub.add_parser(name)
        p.add_argument("--max-pages", type=int, default=None)
        p.add_argument("--max-details", type=int, default=None)
        p.add_argument("--no-seek", action="store_true", help="只处理收件箱")
        p.add_argument("--no-wait", action="store_true", help="排队后立即退出，不等待生成")
        p.add_argument("--mode", choices=("focused", "broad"), default=None,
                       help="focused：graduate/junior 等关键词；broad：珀斯全部 ICT 岗位；默认用已保存的模式")
        p.add_argument("--no-generate", action="store_true",
                       help="只采集和筛选：通过的岗位放进“待生成”，不排队生成材料")
    sub.add_parser("status")
    rescreen = sub.add_parser("rescreen", help="不采集，用现在的规则和匹配度预筛重新筛选台账里已有 JD 的岗位")
    rescreen.add_argument("--status", default=",".join(RESCREEN_EXISTING),
                          help=f"逗号分隔的岗位状态，默认 {','.join(RESCREEN_EXISTING)}")
    rescreen.add_argument("--requeue-failed", action="store_true", help="失败的岗位若通过筛选，重新排队生成")
    rescreen.add_argument("--only-failed", action="store_true", help="仅修复最新筛选中的技术失败；更新待生成状态，不生成材料")
    rescreen.add_argument("--no-wait", action="store_true", help="排队后立即退出；后端下次启动时接着生成")
    mode = sub.add_parser("mode", help="查看或切换已保存的搜索模式")
    mode.add_argument("value", nargs="?", choices=("focused", "broad"))
    args = parser.parse_args(argv)

    settings = Settings()
    engine = make_engine(settings.database_url)
    if settings.auto_create_schema:
        create_schema(engine)
    session_factory = make_session_factory(engine)
    relink_jd_files(session_factory, settings.jobs_dir)
    worker = Worker(settings, session_factory)
    autopilot = Autopilot(settings, session_factory, worker)
    if args.command == "status":
        print(json.dumps(autopilot.status(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "mode":
        print(autopilot.set_mode(args.value) if args.value else autopilot.mode)
        return 0
    worker.start()
    if args.command == "rescreen":
        report = autopilot.rescreen([s.strip() for s in args.status.split(",") if s.strip()],
                                    requeue_failed=args.requeue_failed, only_failed=args.only_failed)
        print_rescreen(report)
        return finish(args, worker, engine, report)
    kwargs = {"trigger": args.command, "mode": args.mode, "generate": False if args.no_generate else None}
    if args.command == "backfill":
        kwargs.update(max_pages=args.max_pages or 3, max_details=args.max_details or 60)
    else:
        kwargs.update(max_pages=args.max_pages, max_details=args.max_details)
    if args.no_seek:
        kwargs["sources"] = ("inbox",)
    report = autopilot.tick(**kwargs)
    print(json.dumps({k: v for k, v in report.items() if k != "traceback"}, ensure_ascii=False, indent=2))
    if report.get("traceback"):
        print(report["traceback"], file=sys.stderr)
    return finish(args, worker, engine, report)


def finish(args, worker, engine, report):
    """Wait for the generations this process queued (unless --no-wait), then close."""
    if not args.no_wait:
        pending = lambda: [t for t in worker.tasks.values() if t.status in ("queued", "running")]  # noqa: E731
        while pending():
            time.sleep(10)
        for task in worker.tasks.values():
            print(f"{task.id} {task.kind} {task.status} {task.jd_name} {task.error or ''}")
    worker.stop()
    engine.dispose()
    return 1 if report["errors"] else 0


DECISIONS = {"skip": "不建议投", "run": "可以投", "run_flagged": "可以投，需核对"}


def print_rescreen(report):
    """One line per job, best first, grouped by the new decision."""
    for note in report["notes"]:
        print("注：" + note)
    for decision in ("run", "run_flagged", "skip"):
        rows = sorted((r for r in report["rescreened"] if r["decision"] == decision), key=lambda r: r["rank"])
        if rows:
            print(f"\n== {DECISIONS[decision]}（{len(rows)}）")
        for r in rows:
            print(f"#{r['job_id']:<3} [{r['status']}] {r['title'][:44]:44} {r['company'][:24]:24} {r['reason'][:80]}")
            if r["flags"]:
                print("      提示：" + "；".join(r["flags"])[:160])
    if report["queued"]:
        print("\n重新排队生成：" + "、".join(r["title"] for r in report["queued"]))
    for error in report["errors"]:
        print("错误：" + error)
    print(f"\n用时 {report['seconds']} 秒；没有 JD、未补筛 {report['no_jd']} 个")


if __name__ == "__main__":
    sys.exit(main())
