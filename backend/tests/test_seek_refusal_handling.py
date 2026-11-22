"""What a refusal does next: it is classified by kind, remembered as a window, and answered with less traffic.

Measured against SEEK's edge on this machine: an even eight-second burst of list pages is refused for ~12-25 minutes by a challenge page carrying no Retry-After, so waiting blindly cannot be the whole answer."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.app.services.autopilot import Autopilot  # noqa: E402
from backend.app.services.sources import seek  # noqa: E402
from backend.tests.test_autopilot import FakeLLM, ROWS, fake_collector_run, settings_for  # noqa: E402
from backend.tests.test_worker import WorkerCase  # noqa: E402


class CollectorFlagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = settings_for(Path(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)

    def test_refusal_handling_is_passed_down_to_the_collector(self):
        options = seek.collector_options(self.settings, "focused")
        self.assertEqual(options[options.index("--transport") + 1], "auto")            # switch transport rather than lose the round
        self.assertEqual(options[options.index("--max-list-per-hour") + 1], "12")      # list pages are what trigger refusals
        self.assertEqual(options[options.index("--max-details-per-hour") + 1], "40")   # details are cheaper and earn the JDs
        self.assertGreaterEqual(float(options[options.index("--jitter-factor") + 1]), 1.0)   # equal gaps read as a script
        self.assertNotIn("--backlog-only", options)                                    # an ordinary round still searches

    def test_a_drain_round_touches_no_search_page(self):
        options = seek.collector_options(self.settings, "broad", backlog_only=True)
        self.assertIn("--backlog-only", options)                                       # owed details only, while the edge objects

    def test_a_non_standard_browser_location_reaches_the_collector(self):
        moved = self.settings.model_copy(update={"chrome_path": Path("D:/Apps/chrome.exe")})
        options = seek.collector_options(moved, "focused")
        self.assertEqual(options[options.index("--browser-path") + 1], str(Path("D:/Apps/chrome.exe")))   # the platform spelling is what the collector receives


class CoolDownStateTests(unittest.TestCase):
    """The collector's own notes beside its cache: how long to wait, and which ids are still owed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.settings = settings_for(self.root)
        (self.root / "collector").mkdir(parents=True, exist_ok=True)

    def block_with(self, text):
        (self.root / "collector" / "seek_block.json").write_text(text, encoding="utf-8")

    def test_an_open_window_holds_off_and_an_expired_one_does_not(self):
        future = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        self.assertFalse(seek.cool_down_open(self.settings))                           # nothing recorded is not a hold-off
        self.block_with(json.dumps({"kind": "challenge", "code": 429, "until": future}))
        self.assertTrue(seek.cool_down_open(self.settings))
        self.block_with(json.dumps({"kind": "throttle", "code": 429, "until": past}))
        self.assertFalse(seek.cool_down_open(self.settings))                           # the window closed: work again

    def test_a_broken_note_never_stops_a_round(self):
        for broken in ['{ half written', '[]', json.dumps({"until": "not a timestamp"})]:   # corrupt or absurd: assume expired
            self.block_with(broken)
            self.assertFalse(seek.cool_down_open(self.settings), broken)

    def test_the_local_backlog_keeps_its_order_and_ignores_junk(self):
        queue = self.root / "collector" / "pending_details.txt"
        self.assertEqual(seek.queued_ids(self.settings), [])                           # nothing promised yet
        queue.write_text("301\n301\n302\nnot-a-number\n", encoding="utf-8")             # order is the promise made to those jobs
        self.assertEqual(seek.queued_ids(self.settings), ["301", "302"])
        queue.write_text("\n".join(str(400 + i) for i in range(60)), encoding="utf-8")
        self.assertEqual(len(seek.queued_ids(self.settings, limit=25)), 25)            # bounded, whatever the file holds

    def test_the_wait_is_shown_on_the_users_own_clock(self):
        until = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
        shown = seek.format_cooldown(until)
        datetime.strptime(shown, "%H:%M")                                              # a wall-clock time, not a raw UTC stamp
        self.assertEqual((seek.format_cooldown(None), seek.format_cooldown("nonsense")), ("", ""))


class RefusalRoundReportTests(WorkerCase):
    """The round's own notes must name what happened: "HTTP 429/403" told the user nothing useful."""

    def tick_with(self, report_fields, block=None, queue=None):
        runner = fake_collector_run(self.root, [dict(r) for r in ROWS[:1]])
        run_dir = next((self.root / "collector" / "seek_output" / "runs").iterdir())
        fields = {"errors": [], **report_fields}
        (run_dir / "run_report.json").write_text(json.dumps(fields), encoding="utf-8")
        collector = self.root / "collector"
        if block is not None:
            (collector / "seek_block.json").write_text(json.dumps(block), encoding="utf-8")
        if queue is not None:
            (collector / "pending_details.txt").write_text("\n".join(queue) + "\n", encoding="utf-8")
        llm = FakeLLM(lambda jd: {"verdict": "unspecified", "quote": "", "reason": "x"})
        autopilot = Autopilot(self.settings, self.sessions, self.worker, launch=lambda path: None,
                              seek_runner=runner, llm_factory=lambda: llm)
        return autopilot.tick(trigger="test", sources=("seek",)), runner

    def test_the_note_names_the_refusal_kind_and_still_states_the_entry_rule(self):
        until = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
        report, _ = self.tick_with(dict(rate_limited=True, blocked_by="challenge", refusal_label="反爬校验拦截", block_code=429,
                                        cooldown_until=until, queue_remaining=203, browser_available=True))
        notes = " ".join(report["notes"])
        self.assertIn("反爬校验拦截（HTTP 429）", notes)                                  # the actual answer, not a guess between two codes
        self.assertNotIn("429/403", notes)                                             # the old blanket wording is gone
        self.assertIn("没有完整 JD 的结果不入账", notes)                                  # the standing rule stays in writing
        self.assertIn("补抓队列", notes)                                                # and unread ids are said to be kept somewhere real
        self.assertIn("203", notes)                                                    # with their count, not an estimate
        self.assertEqual(report["errors"], [])                                          # a refusal is expected weather, not a failure

    def test_a_spent_hourly_budget_is_reported_as_a_pause_not_a_block(self):
        report, _ = self.tick_with(dict(rate_limited=False, quota_exhausted="list"))
        note = next(n for n in report["notes"] if "配额" in n)
        self.assertIn("搜索列表页", note)                                               # which budget ran dry decides what to change
        self.assertIn("这不是访问受限", note)                                            # so it is never mistaken for a refusal

    def test_an_open_window_with_work_owed_reads_details_only(self):
        until = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        _, runner = self.tick_with({"rate_limited": False}, block=dict(kind="challenge", code=429, until=until), queue=["901", "902"])
        self.assertIn("--backlog-only", runner.options[-1])                             # list pages wait; the owed JDs do not

    def test_an_expired_window_searches_again_normally(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        _, runner = self.tick_with({"rate_limited": False}, block=dict(kind="throttle", code=429, until=past), queue=["901"])
        self.assertNotIn("--backlog-only", runner.options[-1])                          # nothing holds us back any more


if __name__ == "__main__":
    unittest.main()
