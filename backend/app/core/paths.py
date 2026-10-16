"""Locate the repository and make the generation modules importable.

The generation engine (match_job, tailor_cv, build_resume, export_docx,
interview_prep, ...) lives at the repository root as plain modules that import
each other by bare name. The backend imports them the same way, so the root
is put on sys.path once, here.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
