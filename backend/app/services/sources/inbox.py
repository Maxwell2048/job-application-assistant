"""The inbox source: any .txt or .md dropped into jobs/inbox is a job.

This is the public, site-independent way in. The file name is the external
id, the first non-empty line the title. Files are left in place; the ledger's
hash check keeps a file from being processed twice."""
from pathlib import Path


def collect(settings):
    inbox = Path(settings.inbox_dir)
    if not inbox.is_dir():
        return []
    items = []
    for path in sorted(inbox.iterdir()):
        if path.suffix.lower() not in (".txt", ".md") or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        if not text.strip():
            continue
        title = next((line.strip() for line in text.splitlines() if line.strip()), path.stem)
        items.append({"source": "inbox", "external_id": path.stem, "title": title[:300], "company": "",
                      "url": "", "location": "", "work_type": "", "listed_at": "", "jd_text": text,
                      "expired": False})
    return items
