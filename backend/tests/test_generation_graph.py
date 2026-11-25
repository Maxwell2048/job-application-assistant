"""The LangGraph engine must behave exactly like the classic one: same CLI
calls in the same order, same stage statuses and task outcome, for every
entry point and branch. It also leaves a graph_trace.json in the run."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.app.services import generation_graph  # noqa: E402
from backend.app.services.pipeline import Pipeline, Task  # noqa: E402
from backend.tests.test_api import FakeRunner  # noqa: E402
from backend.tests.test_autopilot import settings_for  # noqa: E402

RUN_NAME = "20260912-000000-abcdef"
PASTED_JD = ("Junior Software Developer\n\nWe are looking for a junior developer in Perth.\n"
             "Requirements\nPython and SQL skills.\nGood communication.\n")


class GapRunner(FakeRunner):
    """Matching finds no evidence for six required conditions."""

    def __call__(self, args, cwd, task=None):
        result = super().__call__(args, cwd, task)
        if Path(args[0]).name == "match_job.py":
            run = self.outputs_dir / RUN_NAME
            requirements = [{"requirement_id": f"R{i:03}", "category": "technical_skill", "importance": "required"}
                            for i in range(6)]
            matches = [{"requirement_id": r["requirement_id"], "processing_status": "ok", "verdict": "insufficient"}
                       for r in requirements]
            (run / "requirements.json").write_text(json.dumps({"requirements": requirements}), encoding="utf-8")
            (run / "matches.json").write_text(json.dumps({"matches": matches}), encoding="utf-8")
        return result


class EngineParityTests(unittest.TestCase):
    def run_task(self, engine, make_task, runner_kwargs=None, gate=None, prepare=None, runner_cls=FakeRunner):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "jobs").mkdir()
            jd = root / "jobs" / "job.txt"
            jd.write_text("Junior developer\nPython\n", encoding="utf-8")
            settings = settings_for(root).model_copy(update={"generation_engine": engine,
                                                             **({"coverage_gate_max_gap_share": gate} if gate else {})})
            runner = runner_cls(settings.outputs_dir, **(runner_kwargs or {}))
            pipeline = Pipeline(settings, runner=runner)
            task = make_task(jd, settings.outputs_dir / RUN_NAME)
            if prepare:
                prepare(settings.outputs_dir / RUN_NAME)
            if task.kind == "redo":
                pipeline.run_redo(task)
            else:
                pipeline.run_full(task, None if task.jd_path else PASTED_JD)
            trace_path = settings.outputs_dir / RUN_NAME / "graph_trace.json"
            trace = json.loads(trace_path.read_text(encoding="utf-8")) if trace_path.is_file() else None
            stages = [(s["key"], s["status"]) for s in task.stages]
            return {"calls": runner.calls, "stages": stages, "status": task.status}, trace

    def assert_same(self, make_task, **kwargs):
        classic, no_trace = self.run_task("classic", make_task, **kwargs)
        graph, trace = self.run_task("langgraph", make_task, **kwargs)
        self.assertEqual(graph, classic)
        self.assertIsNone(no_trace)            # the classic engine writes no graph trace
        return graph, trace

    @staticmethod
    def existing_run(run_dir):
        """A finished run for redo tasks: what the full pipeline would have left."""
        FakeRunner(run_dir.parent)(["match_job.py", "--jd", "x"], ".")
        for script in ("tailor_cv.py", "build_resume.py"):
            FakeRunner(run_dir.parent)([script], ".")

    def test_full_run_from_a_saved_jd(self):
        result, trace = self.assert_same(lambda jd, run: Task("full", jd_path=jd))
        self.assertEqual(result["status"], "done")
        self.assertEqual([n["node"] for n in trace[-1]["nodes"]], ["match", "gate", "tailor", "build", "export"])
        self.assertEqual(trace[-1]["start"], "match")
        self.assertGreater(trace[-1]["checkpoints"], 5)

    def test_full_run_from_pasted_text_saves_first(self):
        result, trace = self.assert_same(lambda jd, run: Task("full", jd_name="pasted"))
        self.assertEqual(trace[-1]["nodes"][0]["node"], "save")
        self.assertEqual(result["status"], "done")

    def test_interview_branch(self):
        _, trace = self.assert_same(lambda jd, run: Task("full", jd_path=jd, with_interview=True))
        self.assertEqual(trace[-1]["nodes"][-1]["node"], "interview")

    def test_coverage_gate_stops_after_matching(self):
        result, trace = self.assert_same(lambda jd, run: Task("full", jd_path=jd), gate=0.5, runner_cls=GapRunner)
        self.assertEqual(result["status"], "low_coverage")
        self.assertTrue(trace[-1]["gated"])
        self.assertEqual([n["node"] for n in trace[-1]["nodes"]], ["match", "gate"])

    def test_redo_entry_points(self):
        for redo, first in (("tailor", "tailor"), ("closing", "tailor"), ("assemble", "build"), ("export", "export")):
            with self.subTest(redo=redo):
                _, trace = self.assert_same(lambda jd, run, r=redo: Task("redo", run_dir=run, redo=r),
                                            prepare=self.existing_run)
                self.assertEqual(trace[-1]["nodes"][0]["node"], first)

    def test_failed_stage_and_location_block(self):
        result, trace = self.assert_same(lambda jd, run: Task("full", jd_path=jd),
                                         runner_kwargs={"fail_at": "build_resume.py"})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(trace[-1]["nodes"][-1]["node"], "tailor")   # build raised; its node never finished
        result, _ = self.assert_same(lambda jd, run: Task("full", jd_path=jd),
                                     runner_kwargs={"match_status": "partial"})
        self.assertEqual(result["status"], "done")


class GraphShapeTests(unittest.TestCase):
    def test_mermaid_lists_every_node_and_branch(self):
        text = generation_graph.mermaid()
        for node in ("save", "match", "gate", "tailor", "build", "export", "interview"):
            self.assertIn(node, text)
        self.assertIn("stop", text)
        self.assertIn("continue", text)


if __name__ == "__main__":
    unittest.main()
