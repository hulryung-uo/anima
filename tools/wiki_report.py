"""File a wiki discrepancy report into the uowiki repo.

When in-game experience contradicts a wiki page (wrong numbers, missing
mechanics, nonexistent page), this writes a report into
`<wiki>/reports/open/YYYY-MM-DD-<agent>-<slug>.md` using the exact format
from uowiki/CLAUDE.md. The librarian routine triages open reports daily.

Usage:
    python3 tools/wiki_report.py \\
        --agent bjorn \\
        --page src/content/docs/skills/mining.md \\
        --claim "mining gives 2 ore per success" \\
        --observed "got 1 ore per success across 40 swings (logs/bjorn/2026-06-11.log)" \\
        --expected "wiki says 2 ore per success below GM" \\
        --evidence "bjorn 2026-06-11T09:12Z logs/bjorn/2026-06-11.log"

Options:
    --wiki-root PATH   override wiki location (default: ../uowiki next to this repo)
    --force            allow filing against a page that doesn't exist (propose new page)
    --commit           git add + commit the report in the wiki repo (no push)
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

ANIMA_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WIKI_ROOT = ANIMA_ROOT.parent / "uowiki"


def slugify(text: str, max_words: int = 6) -> str:
    """Turn a claim into a kebab-case slug (first few words)."""
    words = re.sub(r"[^a-z0-9\s-]", "", text.lower()).split()
    return "-".join(words[:max_words]) or "report"


def unique_path(directory: Path, stem: str) -> Path:
    """Return <stem>.md in directory, appending -2, -3, ... if taken."""
    path = directory / f"{stem}.md"
    counter = 2
    while path.exists():
        path = directory / f"{stem}-{counter}.md"
        counter += 1
    return path


def build_report(claim: str, page: str, observed: str, expected: str, evidence: str) -> str:
    """Render the report body in the exact uowiki/CLAUDE.md template."""
    return (
        f"# {claim}\n"
        f"- page: {page}\n"
        f"- observed: {observed}\n"
        f"- expected-per-wiki: {expected}\n"
        f"- evidence: {evidence}\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="File a discrepancy report to the uowiki repo")
    parser.add_argument("--agent", required=True, help="Agent name (e.g. bjorn)")
    parser.add_argument("--page", required=True,
                        help="Wiki page path (e.g. src/content/docs/skills/mining.md)")
    parser.add_argument("--claim", required=True, help="Short claim being disputed")
    parser.add_argument("--observed", required=True,
                        help="What actually happened, with log excerpt or code path")
    parser.add_argument("--expected", required=True, help="What the wiki page says")
    parser.add_argument("--evidence", required=True,
                        help="Agent name, timestamp, anima log path or servuo file:line")
    parser.add_argument("--wiki-root", default=str(DEFAULT_WIKI_ROOT),
                        help="Path to the uowiki repo (default: ../uowiki)")
    parser.add_argument("--force", action="store_true",
                        help="File even if --page doesn't exist (propose a new page)")
    parser.add_argument("--commit", action="store_true",
                        help="git add + commit the report in the wiki repo (no push)")
    args = parser.parse_args()

    wiki_root = Path(args.wiki_root).expanduser().resolve()
    if not wiki_root.is_dir():
        sys.exit(f"error: wiki root not found: {wiki_root}")

    page_path = wiki_root / args.page
    if not page_path.is_file():
        if not args.force:
            sys.exit(f"error: page not found under wiki root: {args.page}\n"
                     f"  (use --force to file against a missing page, e.g. to propose a new one)")
        print(f"warning: page not found: {args.page} — filing anyway (--force)")

    open_dir = wiki_root / "reports" / "open"
    open_dir.mkdir(parents=True, exist_ok=True)

    today = dt.date.today().isoformat()
    stem = f"{today}-{args.agent}-{slugify(args.claim)}"
    report_path = unique_path(open_dir, stem)

    report_path.write_text(
        build_report(args.claim, args.page, args.observed, args.expected, args.evidence),
        encoding="utf-8",
    )
    rel = report_path.relative_to(wiki_root)
    print(f"filed: {report_path}")

    if args.commit:
        subprocess.run(["git", "-C", str(wiki_root), "add", str(rel)], check=True)
        subprocess.run(
            ["git", "-C", str(wiki_root), "commit",
             "-m", f"report({args.agent}): {args.claim}", "--", str(rel)],
            check=True,
        )
        print(f"committed in {wiki_root}")


if __name__ == "__main__":
    main()
