"""
run_scrape.py
=============
Runs one LinkedIn scraper pass for every selected job title, then merges the
results into jobs/linkedin_results.md. Launched by the Command Center's
"Run scraper" button (POST /api/pipeline/scrape).

Why this exists: the old launcher was a .bat file that ran the scraper and
merger blind. When a step failed (expired LinkedIn session, no job cards, an
API error while scoring, nothing to merge) nothing was written, the terminal
closed, and the Jobs page waited forever for a results update that never came.

This runner always finishes by writing jobs/scraper_status.json, which the
Command Center watches. The page then stops its spinner and shows what
happened, including the scraper's own error message.

Usage:
    python pipeline/run_scrape.py --spec jobs/.scraper_spec.json

The spec file is JSON: {"queries": [...], "remote": true, "easyApply": true, "pages": 3}
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parent
JOBS_DIR = REPO_ROOT / "jobs"
SENTINEL = JOBS_DIR / ".scraper_running"
STATUS_PATH = JOBS_DIR / "scraper_status.json"
LOG_PATH = JOBS_DIR / "scraper_last_run.log"
RESULTS_PATH = JOBS_DIR / "linkedin_results.md"
SCRAPER = PIPELINE_DIR / "linkedin_scraper.py"
MERGER = PIPELINE_DIR / "merge_linkedin_results.py"

# Exit code linkedin_scraper.py uses when the saved LinkedIn session is gone.
EXIT_NOT_LOGGED_IN = 2

# Known scraper messages, mapped to a short reason the UI can show.
KNOWN_REASONS = [
    ("LinkedIn session not detected", "LinkedIn session expired. Run: python pipeline/auth_linkedin.py, log in, then run the scraper again."),
    ("Playwright is not installed", "Playwright is not installed. Run: pip install playwright && playwright install chromium"),
    ("No new jobs found (all results already applied to)", "Every job found is already in your application tracker."),
    ("No jobs scraped", "LinkedIn returned no job cards. The session may be expired or LinkedIn changed its page layout."),
    ("No job cards found", "LinkedIn returned no job cards. The session may be expired or LinkedIn changed its page layout."),
    ("authentication_error", "The Anthropic API key was rejected while scoring jobs. Check ANTHROPIC_API_KEY in .env."),
    ("credit balance is too low", "The Anthropic account is out of credits, so jobs could not be scored."),
    ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY is missing, so jobs could not be scored."),
    ("ModuleNotFoundError", "A Python package is missing. Run: pip install -r pipeline/requirements.txt"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Tee:
    """Writes every line to the console and to the run log."""

    def __init__(self, log_path: Path):
        self.log = open(log_path, "w", encoding="utf-8", errors="replace")

    def write(self, text: str) -> None:
        try:
            sys.stdout.write(text)
        except UnicodeEncodeError:
            enc = sys.stdout.encoding or "ascii"
            sys.stdout.write(text.encode(enc, errors="replace").decode(enc, errors="replace"))
        sys.stdout.flush()
        self.log.write(text)
        self.log.flush()

    def close(self) -> None:
        self.log.close()


def _child_env() -> dict:
    env = dict(os.environ)
    # Child output is piped, so on Windows Python would fall back to cp1252
    # and crash on any non-ASCII character (job titles, the merger's arrow).
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def run_step(args: list[str], out: Tee) -> tuple[int, str]:
    """Run a Python script, stream its output, return (exit code, output)."""
    captured: list[str] = []
    try:
        proc = subprocess.Popen(
            [sys.executable, *args],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=_child_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as e:
        msg = f"Failed to start {args[0]}: {e}\n"
        out.write(msg)
        return 1, msg
    assert proc.stdout is not None
    for line in proc.stdout:
        captured.append(line)
        out.write(line)
    return proc.wait(), "".join(captured)


def explain(output: str, code: int) -> str:
    for needle, reason in KNOWN_REASONS:
        if needle in output:
            return reason
    if code != 0:
        tail = [l for l in output.strip().splitlines() if l.strip()][-1:] or ["no output"]
        return f"Scraper exited with code {code}: {tail[0][:300]}"
    return "No results were written."


def write_status(status: dict) -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status, indent=2), encoding="utf-8")
    os.replace(tmp, STATUS_PATH)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spec", required=True, help="Path to the JSON spec written by the Command Center.")
    parser.add_argument("--no-pause", action="store_true", help="Never wait for Enter on failure.")
    args = parser.parse_args()

    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    out = Tee(LOG_PATH)
    started = _now()
    status: dict = {"state": "failed", "startedAt": started, "queries": [], "message": ""}

    try:
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        queries = [q.strip() for q in spec.get("queries", []) if isinstance(q, str) and q.strip()]
        if not queries:
            raise ValueError("No search queries in spec.")

        flags: list[str] = []
        if spec.get("remote", True):
            flags.append("--remote")
        if spec.get("easyApply", True):
            flags.append("--easy-apply")
        if spec.get("pages"):
            flags += ["--pages", str(int(spec["pages"]))]

        # Leftovers from an earlier run that died would be merged into this one.
        for old in JOBS_DIR.glob(".tmp_results_*.md"):
            old.unlink(missing_ok=True)

        out.write(f"Running LinkedIn scraper for {len(queries)} search(es)\n\n")
        reasons: list[str] = []
        for i, q in enumerate(queries):
            tmp_out = JOBS_DIR / f".tmp_results_{i}.md"
            out.write(f"=== Searching: {q} ===\n")
            code, output = run_step([str(SCRAPER), "--query", q, *flags, "--output", str(tmp_out)], out)
            wrote = tmp_out.exists()
            entry = {"query": q, "exitCode": code, "wroteResults": wrote}
            if not wrote:
                entry["reason"] = explain(output, code)
                reasons.append(entry["reason"])
            status["queries"].append(entry)
            out.write("\n")
            # An expired session fails every query the same way; stop early.
            if code == EXIT_NOT_LOGGED_IN:
                out.write("Stopping: LinkedIn session is not logged in.\n")
                break

        if any(e["wroteResults"] for e in status["queries"]):
            code, output = run_step([str(MERGER), *queries], out)
            if code != 0:
                status["message"] = explain(output, code)
            else:
                status["state"] = "done"
                total = None
                for line in output.splitlines():
                    if line.startswith("Merged "):
                        try:
                            total = int(line.split()[1])
                        except (IndexError, ValueError):
                            pass
                status["jobsFound"] = total
                status["message"] = f"Found {total} job(s)." if total is not None else "Results updated."
                if reasons:
                    status["message"] += f" {len(reasons)} search(es) returned nothing: {reasons[0]}"
        else:
            # Nothing new: keep the old results file, but say why.
            unique = list(dict.fromkeys(reasons))
            if all(e["exitCode"] == 0 for e in status["queries"]):
                status["state"] = "no_results"
            status["message"] = " ".join(unique) if unique else "No results were written."
    except Exception as e:  # noqa: BLE001 - report anything, never leave the UI hanging
        status["message"] = f"Scrape runner error: {e}"
        out.write(f"\n{status['message']}\n")
    finally:
        status["finishedAt"] = _now()
        out.write(f"\n[{status['state']}] {status['message']}\n")
        out.close()
        write_status(status)
        SENTINEL.unlink(missing_ok=True)

    if status["state"] == "failed" and not args.no_pause and sys.stdin and sys.stdin.isatty():
        try:
            input("\nThe scraper did not finish. Press Enter to close this window...")
        except EOFError:
            pass
    return 0 if status["state"] != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
