#!/usr/bin/env python3
"""
clippy_compare.py
=================

Compare two Clippy outputs using the taxonomy from:

  Tadesse et al., "Code Quality Analysis of Translations from C to Rust" (2026)

Designed for a before/after thesis workflow:
  - "before" = Rust translated from C BEFORE technical-debt remediation
  - "after"  = Rust translated from C AFTER  technical-debt remediation

Produces:
  1. A side-by-side category table (warnings, raw + normalized per-100-LOC)
  2. Internal-vs-External quality summary
  3. Trade-off detector: categories where remediation improved one dimension
     at the cost of another (the paper's headline finding)
  4. Per-lint deltas inside each category (which specific lints changed)

Usage:
    python3 clippy_compare.py BEFORE.txt AFTER.txt \\
        --before-loc 220 --after-loc 215 \\
        [--mapping category_to_lints_mapping.json] \\
        [--norm 100] \\
        [--out report.md]

If --before-loc / --after-loc are omitted, raw counts are shown but the
per-LOC normalization is skipped (the paper's central methodological point
is that raw counts mislead — provide LOC where possible).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Taxonomy bookkeeping
# ---------------------------------------------------------------------------

# Per Table 1 of the paper. "Build configuration issues" appears in the
# supplementary JSON but not in Table 1; we treat it as External Quality
# (it concerns the build environment, not source comprehensibility).
INTERNAL_QUALITY = {
    "Convention violation",
    "Documentation issues",
    "Inflexible code",
    "Misleading code",
    "Non-idiomatic",          # paper's Table 1 says "Non-idiomatic code"; JSON uses "Non-idiomatic"
    "Non-production code",
    "Readability issues",
    "Redundant",              # paper's Table 1 says "Redundant code"; JSON uses "Redundant"
}
EXTERNAL_QUALITY = {
    "Arithmetic issues",
    "Attribute issues",
    "Compatibility issues",
    "Error handling issues",
    "Logical issues",
    "Memory safety",
    "Performance",
    "Runtime panic risks",
    "Thread safety",
    "Type safety",
    "Build configuration issues",
}

ALL_CATEGORIES_ORDERED = [
    # Internal
    "Convention violation",
    "Documentation issues",
    "Inflexible code",
    "Misleading code",
    "Non-idiomatic",
    "Non-production code",
    "Readability issues",
    "Redundant",
    # External
    "Arithmetic issues",
    "Attribute issues",
    "Compatibility issues",
    "Error handling issues",
    "Logical issues",
    "Memory safety",
    "Performance",
    "Runtime panic risks",
    "Thread safety",
    "Type safety",
    "Build configuration issues",
]

# Lints emitted by Clippy/rustc itself that are not in the paper's 782-lint
# universe. We bucket them sensibly so they aren't dropped silently.
FALLBACK_LINT_CATEGORIES = {
    # rustc lints that frequently show up alongside clippy
    "unused_variables": "Redundant",
    "unused_imports": "Redundant",
    "unused_assignments": "Redundant",
    "unused_mut": "Redundant",
    "unused_must_use": "Error handling issues",
    "dead_code": "Redundant",
    "non_snake_case": "Convention violation",
    "non_camel_case_types": "Convention violation",
    "non_upper_case_globals": "Convention violation",
    "deprecated": "Compatibility issues",
    "unreachable_code": "Redundant",
    "unreachable_patterns": "Redundant",
}

# ---------------------------------------------------------------------------
# Clippy plain-text parser
# ---------------------------------------------------------------------------
#
# Plain-text Clippy output (which is what you get from the Rust Playground)
# has lines that look like:
#
#   warning: variable does not need to be mutable
#    --> src/main.rs:12:9
#     |
#  12 |     let mut x = 5;
#     |         ----^
#     |         |
#     |         help: remove this `mut`
#     |
#     = note: `#[warn(unused_mut)]` on by default
#
# The lint name is in either:
#   = note: `#[warn(LINT_NAME)]` on by default
#   = note: `#[warn(clippy::LINT_NAME)]` on by default
#   = help: for further information visit https://rust-lang.github.io/rust-clippy/master/index.html#LINT_NAME
#
# Sometimes Clippy emits a more compact form:
#   = help: ... index.html#LINT_NAME
# We accept both.

LINT_NOTE_RE = re.compile(
    r"=\s*note:\s*`#\[(?:warn|deny|allow|forbid)\((?:clippy::)?([a-zA-Z0-9_]+)\)\]`"
)
LINT_HELP_RE = re.compile(
    r"index\.html#([a-zA-Z0-9_]+)"
)
WARNING_HEADER_RE = re.compile(r"^(warning|error):\s+(.*)$")


def parse_clippy_text(text: str) -> list[dict]:
    """Parse plain-text Clippy output into a list of warning dicts.

    Each warning is delimited by a `warning:` (or `error:`) header line.
    We attribute each warning to a single lint name found inside its block.
    """
    lines = text.splitlines()
    warnings: list[dict] = []
    current: dict | None = None

    for line in lines:
        m = WARNING_HEADER_RE.match(line.strip())
        if m and not line.startswith(" ") and not line.startswith("\t"):
            # New warning block starts
            if current is not None:
                warnings.append(current)
            current = {
                "severity": m.group(1),
                "message": m.group(2).strip(),
                "lint": None,
                "raw": [line],
            }
            continue

        if current is None:
            continue

        current["raw"].append(line)

        # Try to pick up the lint name
        if current["lint"] is None:
            mn = LINT_NOTE_RE.search(line)
            if mn:
                current["lint"] = mn.group(1)
                continue
            mh = LINT_HELP_RE.search(line)
            if mh:
                current["lint"] = mh.group(1)

    if current is not None:
        warnings.append(current)

    # Filter out summary lines like "warning: `foo` (bin "foo") generated 12 warnings..."
    cleaned = []
    summary_re = re.compile(r"generated \d+ warning[s]?\b")
    for w in warnings:
        if summary_re.search(w["message"]):
            continue
        cleaned.append(w)

    # Carry-forward heuristic for collapsed repeat lints.
    # Clippy emits the `= note: #[warn(...)]` line only on the first occurrence
    # of a repeated lint; subsequent warnings with the same message-prefix have
    # lint=None. Map them to the most recent prior warning with a matching
    # message prefix.
    def msg_signature(msg: str) -> str:
        # Take the first ~6 alphanumeric tokens, lowercased, ignoring identifiers
        # in backticks (which differ between occurrences).
        stripped = re.sub(r"`[^`]*`", "`X`", msg)
        toks = re.findall(r"[A-Za-z]+", stripped.lower())
        return " ".join(toks[:6])

    last_lint_for_signature: dict[str, str] = {}
    for w in cleaned:
        sig = msg_signature(w["message"])
        if w["lint"] is not None:
            last_lint_for_signature[sig] = w["lint"]
        elif sig in last_lint_for_signature:
            w["lint"] = last_lint_for_signature[sig]
            w["lint_inferred"] = True

    return cleaned


# ---------------------------------------------------------------------------
# Category mapping
# ---------------------------------------------------------------------------

def load_lint_to_category(mapping_path: Path) -> dict[str, str]:
    with mapping_path.open() as f:
        cat_to_lints = json.load(f)
    lint_to_cat: dict[str, str] = {}
    for cat, lints in cat_to_lints.items():
        for lint in lints:
            lint_to_cat[lint] = cat
    return lint_to_cat


def categorize(
    warnings: list[dict],
    lint_to_cat: dict[str, str],
) -> tuple[Counter, Counter, list[dict]]:
    """Return (category_counts, lint_counts, unmapped_warnings)."""
    cat_counts: Counter = Counter()
    lint_counts: Counter = Counter()
    unmapped: list[dict] = []
    for w in warnings:
        lint = w["lint"]
        if lint is None:
            unmapped.append(w)
            continue
        lint_counts[lint] += 1
        cat = lint_to_cat.get(lint) or FALLBACK_LINT_CATEGORIES.get(lint)
        if cat is None:
            unmapped.append(w)
            continue
        cat_counts[cat] += 1
    return cat_counts, lint_counts, unmapped


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

@dataclass
class Side:
    label: str
    loc: int | None
    warnings: list[dict]
    cat_counts: Counter
    lint_counts: Counter
    unmapped: list[dict]

    def per_norm(self, count: int, norm: int) -> float | None:
        if self.loc is None or self.loc <= 0:
            return None
        return count * norm / self.loc


def build_side(label: str, text: str, loc: int | None,
               lint_to_cat: dict[str, str]) -> Side:
    warnings = parse_clippy_text(text)
    cat_counts, lint_counts, unmapped = categorize(warnings, lint_to_cat)
    return Side(label, loc, warnings, cat_counts, lint_counts, unmapped)


# ---------------------------------------------------------------------------
# Trade-off detector
# ---------------------------------------------------------------------------
#
# The paper's signature finding: "improvements in one dimension are often
# offset by regressions in others." We surface this for two-way compares.
#
# Definition (inspired by the paper's qualitative narrative):
#   Compare normalized rates (per N LOC). For each category compute
#   delta = after - before. A "trade-off" is observed if BOTH:
#     - some category has delta <= -IMPROVE_THRESH (improvement, after lower)
#     - some category has delta >= +REGRESS_THRESH (regression, after higher)
#
# We then highlight the largest improvement and largest regression and ask
# whether they sit on opposite sides of the internal/external boundary,
# which is where the paper's most interesting trade-offs occurred (e.g.,
# C2SaferRust improving idiomaticity while inflating thread-safety risks).

IMPROVE_THRESH_DEFAULT = 1.0   # warnings per 100 LOC
REGRESS_THRESH_DEFAULT = 1.0


def category_deltas(before: Side, after: Side, norm: int) -> list[dict]:
    rows = []
    for cat in ALL_CATEGORIES_ORDERED:
        b_raw = before.cat_counts.get(cat, 0)
        a_raw = after.cat_counts.get(cat, 0)
        b_norm = before.per_norm(b_raw, norm)
        a_norm = after.per_norm(a_raw, norm)
        delta_norm = (a_norm - b_norm) if (b_norm is not None and a_norm is not None) else None
        delta_raw = a_raw - b_raw
        rows.append({
            "category": cat,
            "quality": "Internal" if cat in INTERNAL_QUALITY else "External",
            "before_raw": b_raw,
            "after_raw": a_raw,
            "before_norm": b_norm,
            "after_norm": a_norm,
            "delta_raw": delta_raw,
            "delta_norm": delta_norm,
        })
    return rows


def detect_tradeoffs(rows: list[dict],
                     improve_thresh: float,
                     regress_thresh: float) -> dict:
    improved = [r for r in rows
                if r["delta_norm"] is not None and r["delta_norm"] <= -improve_thresh]
    regressed = [r for r in rows
                 if r["delta_norm"] is not None and r["delta_norm"] >= regress_thresh]
    improved.sort(key=lambda r: r["delta_norm"])           # most negative first
    regressed.sort(key=lambda r: -r["delta_norm"])         # most positive first
    cross_quality = bool(
        improved and regressed
        and improved[0]["quality"] != regressed[0]["quality"]
    )
    return {
        "improved": improved,
        "regressed": regressed,
        "cross_quality_tradeoff": cross_quality,
        "biggest_improvement": improved[0] if improved else None,
        "biggest_regression": regressed[0] if regressed else None,
    }


def per_lint_deltas(before: Side, after: Side, lint_to_cat: dict[str, str]) -> list[dict]:
    """Return per-lint deltas, sorted by absolute change magnitude."""
    all_lints = set(before.lint_counts) | set(after.lint_counts)
    rows = []
    for lint in all_lints:
        b = before.lint_counts.get(lint, 0)
        a = after.lint_counts.get(lint, 0)
        if b == a:
            continue
        rows.append({
            "lint": lint,
            "category": lint_to_cat.get(lint) or FALLBACK_LINT_CATEGORIES.get(lint) or "(unmapped)",
            "before": b,
            "after": a,
            "delta": a - b,
        })
    rows.sort(key=lambda r: -abs(r["delta"]))
    return rows


# ---------------------------------------------------------------------------
# Reporting (Markdown)
# ---------------------------------------------------------------------------

def fmt(x, prec=2):
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.{prec}f}"
    return str(x)


def render_report(before: Side, after: Side, norm: int,
                  improve_thresh: float, regress_thresh: float,
                  lint_to_cat: dict[str, str]) -> str:
    rows = category_deltas(before, after, norm)
    tradeoffs = detect_tradeoffs(rows, improve_thresh, regress_thresh)
    lint_rows = per_lint_deltas(before, after, lint_to_cat)

    out = []
    out.append("# Clippy Comparison: Before vs. After Technical-Debt Remediation\n")
    out.append("Methodology adapted from Tadesse et al., *Code Quality Analysis of "
               "Translations from C to Rust* (2026). Lints are bucketed into the "
               "paper's 18-category taxonomy and split into Internal vs. External "
               "quality. Counts are normalized per LOC because raw counts mislead "
               "when the two translations differ in size.\n")

    # ---- Summary ----
    out.append("## 1. Summary\n")
    b_loc = before.loc if before.loc is not None else "?"
    a_loc = after.loc if after.loc is not None else "?"
    out.append(f"- **Before:** {len(before.warnings)} warnings over {b_loc} LOC")
    out.append(f"- **After:**  {len(after.warnings)} warnings over {a_loc} LOC")
    if before.loc and after.loc:
        b_rate = len(before.warnings) * norm / before.loc
        a_rate = len(after.warnings) * norm / after.loc
        out.append(f"- **Total warning rate (per {norm} LOC):** "
                   f"{b_rate:.2f} → {a_rate:.2f} (Δ = {a_rate - b_rate:+.2f})")
    out.append("")

    int_b = sum(r["before_raw"] for r in rows if r["quality"] == "Internal")
    int_a = sum(r["after_raw"] for r in rows if r["quality"] == "Internal")
    ext_b = sum(r["before_raw"] for r in rows if r["quality"] == "External")
    ext_a = sum(r["after_raw"] for r in rows if r["quality"] == "External")
    out.append("| Quality dimension | Before (raw) | After (raw) | Δ |")
    out.append("|---|---:|---:|---:|")
    out.append(f"| Internal Quality | {int_b} | {int_a} | {int_a - int_b:+d} |")
    out.append(f"| External Quality | {ext_b} | {ext_a} | {ext_a - ext_b:+d} |")
    out.append("")

    # ---- Per-category table ----
    out.append("## 2. Per-Category Breakdown\n")
    norm_label = f"per {norm} LOC"
    out.append(f"| Category | Quality | Before raw | After raw | "
               f"Before {norm_label} | After {norm_label} | Δ {norm_label} |")
    out.append("|---|---|---:|---:|---:|---:|---:|")
    for r in rows:
        if r["before_raw"] == 0 and r["after_raw"] == 0:
            continue
        out.append(
            f"| {r['category']} | {r['quality']} | "
            f"{r['before_raw']} | {r['after_raw']} | "
            f"{fmt(r['before_norm'])} | {fmt(r['after_norm'])} | "
            f"{fmt(r['delta_norm'])} |"
        )
    # Also list categories with zero on both sides as a sanity footnote.
    zeros = [r["category"] for r in rows if r["before_raw"] == 0 and r["after_raw"] == 0]
    if zeros:
        out.append("")
        out.append(f"_Categories with zero warnings on both sides_: {', '.join(zeros)}.")
        out.append("Per the paper, Clippy structurally underreports several External Quality "
                   "categories (Compatibility, Error handling, Runtime panic risks, Thread safety) "
                   "in C-style Rust — absence here does not imply absence in the code.")
    out.append("")

    # ---- Trade-off detection ----
    out.append("## 3. Trade-Off Analysis\n")
    out.append(f"Thresholds: improvement ≤ −{improve_thresh:.2f} per {norm} LOC, "
               f"regression ≥ +{regress_thresh:.2f} per {norm} LOC.\n")

    if tradeoffs["improved"] and tradeoffs["regressed"]:
        out.append("**Trade-off detected.** Remediation improved some dimensions while "
                   "regressing others — this is the central pattern the paper documents "
                   "across automated translation techniques.\n")
        if tradeoffs["cross_quality_tradeoff"]:
            bi = tradeoffs["biggest_improvement"]
            br = tradeoffs["biggest_regression"]
            out.append(f"> **Cross-quality trade-off:** the biggest improvement is in "
                       f"*{bi['category']}* ({bi['quality']} Quality) but the biggest "
                       f"regression is in *{br['category']}* ({br['quality']} Quality). "
                       f"This is exactly the kind of swap the paper flags as concerning "
                       f"(e.g., gaining idiomaticity at the cost of safety).\n")
        out.append("**Improvements (after < before):**\n")
        for r in tradeoffs["improved"]:
            out.append(f"- {r['category']} ({r['quality']}): "
                       f"{fmt(r['before_norm'])} → {fmt(r['after_norm'])} "
                       f"(Δ = {fmt(r['delta_norm'])})")
        out.append("\n**Regressions (after > before):**\n")
        for r in tradeoffs["regressed"]:
            out.append(f"- {r['category']} ({r['quality']}): "
                       f"{fmt(r['before_norm'])} → {fmt(r['after_norm'])} "
                       f"(Δ = {fmt(r['delta_norm'])})")
    elif tradeoffs["improved"]:
        out.append("**Net improvement, no significant regressions.** Remediation reduced "
                   "warnings in one or more categories without crossing the regression threshold "
                   "in any other. This is the desirable outcome the paper finds rare in practice.\n")
        for r in tradeoffs["improved"]:
            out.append(f"- {r['category']} ({r['quality']}): "
                       f"{fmt(r['before_norm'])} → {fmt(r['after_norm'])} "
                       f"(Δ = {fmt(r['delta_norm'])})")
    elif tradeoffs["regressed"]:
        out.append("**Net regression.** Remediation introduced new warning concentrations "
                   "without offsetting improvements above threshold.\n")
        for r in tradeoffs["regressed"]:
            out.append(f"- {r['category']} ({r['quality']}): "
                       f"{fmt(r['before_norm'])} → {fmt(r['after_norm'])} "
                       f"(Δ = {fmt(r['delta_norm'])})")
    else:
        out.append("No category crossed the improvement or regression threshold. "
                   "The two outputs are quality-equivalent at the chosen sensitivity.\n")
    out.append("")

    # ---- Per-lint deltas ----
    out.append("## 4. Per-Lint Deltas (top 25 by magnitude)\n")
    if not lint_rows:
        out.append("No per-lint changes.")
    else:
        out.append("| Lint | Category | Before | After | Δ |")
        out.append("|---|---|---:|---:|---:|")
        for r in lint_rows[:25]:
            out.append(f"| `{r['lint']}` | {r['category']} | "
                       f"{r['before']} | {r['after']} | {r['delta']:+d} |")
    out.append("")

    # ---- Caveats ----
    out.append("## 5. Caveats\n")
    out.append("- Warnings are not defects. Treat counts as quality *signals*, not verdicts.")
    out.append("- The paper finds Clippy systematically misses certain External Quality issues "
               "in C-style Rust translations (raw pointers, `static mut`, etc.). A zero in "
               "those categories is consistent with that blind spot.")
    out.append("- Per-LOC normalization is essential when before/after differ in size, but "
               "tiny LOC denominators (e.g., < 50) can amplify noise.")
    if before.unmapped or after.unmapped:
        out.append(f"- Unmapped warnings (no recognized lint name): "
                   f"{len(before.unmapped)} before, {len(after.unmapped)} after.")
    out.append("")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("before", help="Path to plain-text Clippy output for the BEFORE side")
    ap.add_argument("after",  help="Path to plain-text Clippy output for the AFTER side")
    ap.add_argument("--before-loc", type=int, default=None,
                    help="Lines of code for the BEFORE Rust translation")
    ap.add_argument("--after-loc",  type=int, default=None,
                    help="Lines of code for the AFTER Rust translation")
    ap.add_argument("--mapping",
                    default=str(Path(__file__).parent / "category_to_lints_mapping.json"),
                    help="Path to category_to_lints_mapping.json from the paper's supp. material")
    ap.add_argument("--norm", type=int, default=100,
                    help="LOC normalization base (paper uses 1000; 100 is more readable "
                         "for Playground-scale snippets)")
    ap.add_argument("--improve-thresh", type=float, default=IMPROVE_THRESH_DEFAULT,
                    help="Improvement threshold (per --norm LOC) for trade-off detection")
    ap.add_argument("--regress-thresh", type=float, default=REGRESS_THRESH_DEFAULT,
                    help="Regression threshold (per --norm LOC) for trade-off detection")
    ap.add_argument("--out", default=None, help="Write Markdown report to this path")
    args = ap.parse_args()

    mapping_path = Path(args.mapping)
    if not mapping_path.exists():
        print(f"ERROR: mapping file not found: {mapping_path}", file=sys.stderr)
        print("Download from the paper's Zenodo record (10.5281/zenodo.17102786) and "
              "extract category_to_lints_mapping.json.", file=sys.stderr)
        return 2

    lint_to_cat = load_lint_to_category(mapping_path)

    before_text = Path(args.before).read_text(encoding="utf-8", errors="replace")
    after_text  = Path(args.after).read_text(encoding="utf-8", errors="replace")

    before = build_side("before", before_text, args.before_loc, lint_to_cat)
    after  = build_side("after",  after_text,  args.after_loc,  lint_to_cat)

    report = render_report(before, after, args.norm,
                           args.improve_thresh, args.regress_thresh, lint_to_cat)

    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
