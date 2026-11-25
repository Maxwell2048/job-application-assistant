"""The generation stages as a LangGraph state graph.

Nodes are the Pipeline's own stage methods (save, match, tailor, build,
export, interview); the graph owns the control flow that used to be written
out by hand:

- the entry point depends on the task: a full run starts at save or match,
  a redo starts at tailor (also "closing"), build ("assemble") or export;
- after matching, the coverage gate either stops the run with the match
  report or continues to tailoring;
- interview preparation runs only for a full task that asked for it.

Exceptions (cancel, location block, stage failure) leave the graph unchanged
and are handled by the Pipeline exactly as in the classic engine. Each run is
checkpointed in memory under the task id, and the visited nodes with their
timings are written to the run directory as graph_trace.json.

    python -m backend.app.services.generation_graph   # print the graph as Mermaid
"""
import json
import operator
import time
from pathlib import Path
from typing import Annotated, Optional, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

GRAPH_VERSION = "1"
ENTRY = {"match": "match", "tailor": "tailor", "closing": "tailor", "assemble": "build", "export": "export"}


class GenerationState(TypedDict, total=False):
    start: str                 # "match" for a full task, else the redo step
    gated: bool                # stopped by the coverage gate after matching
    trace: Annotated[list, operator.add]   # visited nodes, appended by each node


def _entry(task, state):
    start = state["start"]
    if start == "match" and task.jd_path is None:
        return "save"
    return ENTRY[start]


def build_graph(pipeline, task, jd_text=None, checkpointer=None):
    """Compile the graph for one task. Nodes close over the pipeline and task;
    the state carries only what decides the route."""

    def node(name, work):
        def run(state):
            started = time.time()
            update = work(state) or {}
            return {**update, "trace": [{"node": name, "seconds": round(time.time() - started, 1)}]}
        return run

    def tailor(state):
        pipeline._tailor_step(task, "closing" if state["start"] == "closing" else "tailor")

    def gate(state):
        return {"gated": bool(pipeline._below_gate(task))}

    graph = StateGraph(GenerationState)
    graph.add_node("save", node("save", lambda s: pipeline._save_step(task, jd_text)))
    graph.add_node("match", node("match", lambda s: pipeline._match_step(task)))
    graph.add_node("gate", node("gate", gate))
    graph.add_node("tailor", node("tailor", tailor))
    graph.add_node("build", node("build", lambda s: pipeline._build_step(task)))
    graph.add_node("export", node("export", lambda s: pipeline._docx_step(task)))
    graph.add_node("interview", node("interview", lambda s: pipeline._interview_step(task)))

    graph.add_conditional_edges(START, lambda s: _entry(task, s),
                                {"save": "save", "match": "match", "tailor": "tailor", "build": "build",
                                 "export": "export"})
    graph.add_edge("save", "match")
    graph.add_edge("match", "gate")
    graph.add_conditional_edges("gate", lambda s: "stop" if s.get("gated") else "continue",
                                {"stop": END, "continue": "tailor"})
    graph.add_edge("tailor", "build")
    graph.add_edge("build", "export")
    graph.add_conditional_edges(
        "export", lambda s: "interview" if task.kind == "full" and task.with_interview else "end",
        {"interview": "interview", "end": END})
    graph.add_edge("interview", END)
    return graph.compile(checkpointer=checkpointer)


def run_graph(pipeline, task, start, jd_text=None):
    """Run one task through the graph; returns the final state."""
    checkpointer = InMemorySaver()
    config = {"configurable": {"thread_id": task.id}}
    app = build_graph(pipeline, task, jd_text, checkpointer)
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        return app.invoke({"start": start, "trace": []}, config)
    finally:
        _write_trace(task, app, config, start, started)


def _write_trace(task, app, config, start, started):
    """graph_trace.json in the run directory: entry, visited nodes and timings,
    and how many checkpoints the run produced. Never fails the task."""
    run_dir: Optional[Path] = Path(task.run_dir) if task.run_dir else None
    if run_dir is None or not run_dir.is_dir():
        return
    try:
        snapshot = app.get_state(config)
        values = snapshot.values or {}
        trace = {"graph_version": GRAPH_VERSION, "engine": "langgraph", "task_id": task.id, "kind": task.kind,
                 "start": start, "started_at": started, "nodes": values.get("trace", []),
                 "gated": bool(values.get("gated")), "next": list(snapshot.next or []),
                 "checkpoints": sum(1 for _ in app.get_state_history(config))}
        path = run_dir / "graph_trace.json"
        history = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        history = history if isinstance(history, list) else [history]
        path.write_text(json.dumps(history + [trace], ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 - the trace is a record, not a stage
        pass


def mermaid():
    """The graph's shape as Mermaid text (no task needed)."""
    class _Stub:
        id, kind, with_interview, jd_path, run_dir = "diagram", "full", True, None, None

    return build_graph(_Stub(), _Stub()).get_graph().draw_mermaid()


if __name__ == "__main__":
    print(mermaid())
