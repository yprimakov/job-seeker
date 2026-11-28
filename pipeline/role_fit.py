"""
role_fit.py
===========
Local, rule-based role fit scorer. No API calls, no network, stdlib only.

Given a job posting's text and a criteria file, it returns a 0-100 fit score
with the reasons behind it and any red flags (for example "in office 5 days a
week" when you only accept remote or hybrid).

Criteria live in a JSON file. Copy the example and edit your own copy, which
is gitignored:

    cp pipeline/role_fit_criteria.example.json pipeline/role_fit_criteria.json

Usage:
    python pipeline/role_fit.py --jd posting.txt
    python pipeline/role_fit.py --jd posting.txt --criteria my_criteria.json --json

Pipeline hook: set ROLE_FIT_ENABLED=1 and process_queue.py scores each job
description before tailoring. If the criteria set "skip_below" and a job scores
under it, the item is marked "skipped" and is not tailored. With the flag off
(the default) nothing changes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

PIPELINE_DIR = Path(__file__).parent
DEFAULT_CRITERIA = PIPELINE_DIR / "role_fit_criteria.json"
EXAMPLE_CRITERIA = PIPELINE_DIR / "role_fit_criteria.example.json"

# Points per component. They add up to 100 before penalties.
WEIGHTS = {"role": 35, "skills": 20, "work_mode": 25, "comp": 15, "seniority": 5}

WORD_NUMS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}

# Built-in red flags that apply whatever the criteria say.
BUILTIN_FLAGS: list[tuple[str, str]] = [
    (r"\bcommission[- ]only\b", "Commission-only pay"),
    (r"\bunpaid\b", "Unpaid role"),
    (r"\b(?:must|required to|willing(?:ness)? to) relocate\b|\brelocation (?:is )?required\b", "Relocation required"),
    (r"\btravel (?:up to )?(?:[5-9]\d|100)\s*%|\b(?:[5-9]\d|100)\s*% travel\b", "Heavy travel (50%+)"),
    (r"\b(?:active )?(?:security|secret|ts/sci) clearance (?:is )?required\b|\bmust (?:have|hold) (?:an? )?(?:active )?(?:security )?clearance\b", "Security clearance required"),
    (r"\b24/7 on[- ]call\b", "24/7 on-call"),
]


@dataclass
class FitResult:
    score: int
    verdict: str
    reasons: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    signals: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Criteria
# ---------------------------------------------------------------------------

def load_criteria(path: Path | None = None) -> dict:
    """Load criteria JSON. Falls back to the example file if no path is given
    and the personal file does not exist."""
    if path is None:
        path = DEFAULT_CRITERIA if DEFAULT_CRITERIA.exists() else EXAMPLE_CRITERIA
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Signal extraction
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("–", "-").replace("—", "-")).strip()


def _has(text: str, phrase: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase.lower()) + r"(?![a-z0-9])", text) is not None


def detect_work_mode(text: str) -> tuple[str, int | None]:
    """Returns (mode, office_days). mode is remote | hybrid | onsite | unknown."""
    t = text.lower()
    day_word = r"(\d|one|two|three|four|five)"
    m = re.search(
        day_word + r"\s*(?:\+\s*)?days?\s*(?:a|per|each|/)\s*week\s*(?:in|at|on)[- ]?(?:the\s+|our\s+)?(?:office|site|onsite|on-site|hq)",
        t,
    ) or re.search(
        r"(?:in|at)[- ](?:the\s+)?office\s*" + day_word + r"\s*days?\s*(?:a|per)\s*week", t
    )
    if m:
        raw = m.group(1)
        days = int(raw) if raw.isdigit() else WORD_NUMS[raw]
        return ("onsite" if days >= 5 else "hybrid"), days
    if re.search(
        r"\b(?:fully|100%|full[- ]time)\s+(?:on[- ]?site|in[- ]office|in[- ]person)\b|\bon[- ]?site only\b|"
        r"\bnot (?:a )?remote\b|\bno remote\b|\bremote work is not\b|\bin[- ]office (?:role|position)\b",
        t,
    ):
        return "onsite", 5
    if re.search(r"\bhybrid\b", t):
        return "hybrid", None
    if re.search(r"\bremote\b|\bwork from home\b|\bwfh\b", t):
        return "remote", 0
    if re.search(r"\bon[- ]?site\b|\bin[- ]office\b|\bin[- ]person\b", t):
        return "onsite", None
    return "unknown", None


_MONEY = r"\$\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kK])?"


def _money(num: str, k: str | None) -> float:
    v = float(num.replace(",", ""))
    return v * 1000 if k else v


def detect_comp(text: str) -> tuple[float, float] | None:
    """Returns (low, high) annual USD, or None if no pay is listed."""
    t = _norm(text)
    rng = re.search(_MONEY + r"\s*(?:-|to)\s*" + _MONEY + r"(\s*(?:/|per|an|a)\s*(?:hour|hr))?", t)
    if rng:
        lo = _money(rng.group(1), rng.group(2))
        hi = _money(rng.group(3), rng.group(4) or rng.group(2))
        if rng.group(5) or hi < 1000:
            lo, hi = lo * 2080, hi * 2080
        if hi < lo:
            lo, hi = hi, lo
        return lo, hi
    one = re.search(_MONEY + r"(\s*(?:/|per|an|a)\s*(?:hour|hr))?", t)
    if one:
        v = _money(one.group(1), one.group(2))
        if one.group(3):
            v *= 2080
        if v >= 20000:  # ignore small amounts like "$500 stipend"
            return v, v
    return None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_posting(text: str, criteria: dict, title: str = "") -> FitResult:
    body = _norm(text).lower()
    head = _norm(title).lower() or body[:200]
    reasons: list[str] = []
    flags: list[str] = []
    pts: dict[str, float] = {}

    # Role keywords
    role = criteria.get("role_keywords", {})
    must = [k.lower() for k in role.get("must_have_any", [])]
    in_title = [k for k in must if _has(head, k)]
    in_body = [k for k in must if _has(body, k)]
    if not must:
        pts["role"] = WEIGHTS["role"] * 0.6
    elif in_title:
        pts["role"] = WEIGHTS["role"]
        reasons.append(f"Title matches a target role ({in_title[0]})")
    elif in_body:
        pts["role"] = WEIGHTS["role"] * 0.6
        reasons.append(f"Target role mentioned in the description ({in_body[0]}), not the title")
    else:
        pts["role"] = 0
        flags.append("None of your target role keywords appear")

    # Skills
    nice = [k.lower() for k in role.get("nice_to_have", [])]
    hits = [k for k in nice if _has(body, k)]
    pts["skills"] = WEIGHTS["skills"] * (len(hits) / len(nice)) if nice else WEIGHTS["skills"] * 0.5
    if nice:
        reasons.append(f"Skills match {len(hits)}/{len(nice)}" + (f": {', '.join(hits[:6])}" if hits else ""))

    # Work mode
    wm = criteria.get("work_mode", {})
    accept = [m.lower() for m in wm.get("accept", ["remote", "hybrid", "onsite"])]
    max_days = wm.get("max_office_days")
    mode, days = detect_work_mode(body)
    w = WEIGHTS["work_mode"]
    if mode == "unknown":
        pts["work_mode"] = w * 0.5
        reasons.append("Work mode not stated")
    elif mode not in accept:
        pts["work_mode"] = 0
        flags.append("In office 5 days a week" if mode == "onsite" and (days or 5) >= 5 else f"{mode.capitalize()} role, which you do not accept")
    elif mode == "hybrid" and max_days is not None and days is not None and days > max_days:
        pts["work_mode"] = w * 0.2
        flags.append(f"Hybrid with {days} office days a week (your max is {max_days})")
    else:
        pts["work_mode"] = w if mode == "remote" else w * 0.8
        reasons.append(f"Work mode: {mode}" + (f", {days} office days a week" if mode == "hybrid" and days else ""))
    if mode in ("hybrid", "onsite") and wm.get("office_locations"):
        locs = [l.lower() for l in wm["office_locations"]]
        if not any(_has(body, l) for l in locs):
            pts["work_mode"] *= 0.5
            flags.append("Office is not in one of your listed locations")

    # Compensation
    comp_cfg = criteria.get("compensation", {})
    min_base = comp_cfg.get("min_base_usd")
    comp = detect_comp(text)
    c = WEIGHTS["comp"]
    if comp is None:
        pts["comp"] = c * (0.2 if comp_cfg.get("penalize_missing") else 0.5)
        reasons.append("No pay range listed")
    elif not min_base:
        pts["comp"] = c
        reasons.append(f"Pay listed: ${comp[0]:,.0f} to ${comp[1]:,.0f}")
    elif comp[0] >= min_base:
        pts["comp"] = c
        reasons.append(f"Pay ${comp[0]:,.0f} to ${comp[1]:,.0f} meets your minimum")
    elif comp[1] >= min_base:
        pts["comp"] = c * 0.6
        reasons.append(f"Pay ${comp[0]:,.0f} to ${comp[1]:,.0f} only reaches your minimum at the top")
    else:
        pts["comp"] = 0
        flags.append(f"Pay tops out at ${comp[1]:,.0f}, under your ${min_base:,.0f} minimum")

    # Seniority
    sen = criteria.get("seniority", {})
    s = WEIGHTS["seniority"]
    bad = [k for k in sen.get("reject", []) if _has(head, k.lower())]
    good = [k for k in sen.get("accept", []) if _has(head, k.lower())]
    if bad:
        pts["seniority"] = 0
        flags.append(f"Seniority below target ({bad[0]})")
    elif good:
        pts["seniority"] = s
        reasons.append(f"Seniority: {good[0]}")
    else:
        pts["seniority"] = s * 0.6

    # Penalties: criteria avoid-list plus built-in flags
    penalty = 0
    for k in criteria.get("avoid_keywords", []):
        if _has(body, k.lower()):
            flags.append(f"Mentions \"{k}\"")
            penalty += 15
    for pattern, label in BUILTIN_FLAGS:
        if re.search(pattern, body) and label not in flags:
            flags.append(label)
            penalty += 10

    raw = sum(pts.values()) - min(penalty, 40)
    score = max(0, min(100, round(raw)))
    strong = criteria.get("strong_at", 75)
    maybe = criteria.get("maybe_at", 50)
    verdict = "strong fit" if score >= strong else "maybe" if score >= maybe else "weak fit"
    return FitResult(
        score=score,
        verdict=verdict,
        reasons=reasons,
        red_flags=flags,
        signals={
            "work_mode": mode,
            "office_days": days,
            "comp_annual_usd": list(comp) if comp else None,
            "points": {k: round(v, 1) for k, v in pts.items()},
            "penalty": min(penalty, 40),
        },
    )


def format_result(r: FitResult, label: str = "") -> str:
    lines = [f"{label + ': ' if label else ''}{r.score}/100 ({r.verdict})"]
    lines += [f"  + {x}" for x in r.reasons]
    lines += [f"  ! {x}" for x in r.red_flags]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score a job posting against your criteria (local, no API).")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--jd", type=Path, help="Path to job posting text file")
    src.add_argument("--jd-text", type=str, help="Job posting as a string")
    ap.add_argument("--title", default="", help="Job title, if not at the top of the text")
    ap.add_argument("--criteria", type=Path, default=None, help="Criteria JSON (default: role_fit_criteria.json, else the example)")
    ap.add_argument("--json", action="store_true", help="Print JSON instead of text")
    args = ap.parse_args(argv)
    text = args.jd.read_text(encoding="utf-8") if args.jd else args.jd_text
    result = score_posting(text, load_criteria(args.criteria), title=args.title)
    print(json.dumps(result.to_dict(), indent=2) if args.json else format_result(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
