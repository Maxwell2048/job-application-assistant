"""Has code this process already loaded changed on disk since?

Generation stages run as subprocesses and read the engine scripts afresh, but
screening, the API and the autopilot run inside the backend process: an edit
to them takes effect only after a restart. The health check reports the
changed files so the page can say so instead of silently running old rules.
"""
import sys
import threading
from pathlib import Path

_lock = threading.Lock()
_seen = {}          # path -> mtime when first seen loaded


def _project_modules(root):
    root = Path(root).resolve()
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if not path or not path.endswith(".py"):
            continue
        try:
            resolved = Path(path).resolve()
        except OSError:
            continue
        if root in resolved.parents and ".venv" not in resolved.parts:
            yield resolved


def changed_files(root):
    """Project files loaded in this process whose modification time differs from
    when they were first seen, relative to root. Modules loaded later are
    recorded at the time this first notices them."""
    root = Path(root).resolve()
    changed = []
    with _lock:
        for path in _project_modules(root):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            first = _seen.setdefault(path, mtime)
            if mtime != first:
                changed.append(path.relative_to(root).as_posix())
    return sorted(changed)
