#!/usr/bin/env python3
"""Build a deterministic, offline quality report from an authorized corpus."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_HUMAN_FIELDS = ("adequacy", "fluency", "tts_naturalness", "dialogue_leak")


def _words(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise ValueError("reference_vi/hypothesis_vi must be strings")
    return tuple(value.casefold().split())


def _edit_distance(left: Sequence[str], right: Sequence[str]) -> int:
    previous = list(range(len(right) + 1))
    for row, source in enumerate(left, start=1):
        current = [row]
        for column, target in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (source != target),
                )
            )
        previous = current
    return previous[-1]


def build_report(cases: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate text, glossary and bounded human-listening measurements."""

    if not cases:
        raise ValueError("quality corpus must contain at least one case")
    edits = 0
    reference_words = 0
    glossary_total = 0
    glossary_matches = 0
    human_values: dict[str, list[float]] = {field: [] for field in _HUMAN_FIELDS}
    case_reports: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in cases:
        case_id = raw.get("id")
        if not isinstance(case_id, str) or not case_id.strip() or case_id in seen_ids:
            raise ValueError("every quality case needs a unique non-empty id")
        seen_ids.add(case_id)
        reference = _words(raw.get("reference_vi"))
        hypothesis = _words(raw.get("hypothesis_vi"))
        edits_for_case = _edit_distance(reference, hypothesis)
        edits += edits_for_case
        reference_words += len(reference)
        glossary = raw.get("glossary", {})
        if not isinstance(glossary, dict):
            raise ValueError("glossary must be an object")
        normalized_hypothesis = " ".join(hypothesis)
        matched = 0
        for target in glossary.values():
            normalized_target = " ".join(_words(target))
            glossary_total += 1
            if normalized_target and normalized_target in normalized_hypothesis:
                glossary_matches += 1
                matched += 1
        human = raw.get("human", {})
        if human is not None and not isinstance(human, dict):
            raise ValueError("human scores must be an object")
        for field in _HUMAN_FIELDS:
            value = (human or {}).get(field)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 5:
                raise ValueError(f"human.{field} must be between 1 and 5")
            human_values[field].append(float(value))
        case_reports.append(
            {
                "id": case_id,
                "word_edits": edits_for_case,
                "reference_words": len(reference),
                "glossary_matches": matched,
                "glossary_terms": len(glossary),
            }
        )
    human_report = {
        field: round(sum(values) / len(values), 4)
        for field, values in human_values.items()
        if values
    }
    return {
        "schema_version": 1,
        "cases": len(cases),
        "word_error_rate": round(edits / max(reference_words, 1), 6),
        "glossary_compliance": round(glossary_matches / max(glossary_total, 1), 6),
        "human": human_report,
        "case_results": case_reports,
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".part", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    document = json.loads(args.corpus.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("quality corpus schema_version must be 1")
    cases = document.get("cases")
    if not isinstance(cases, list):
        raise ValueError("quality corpus cases must be an array")
    _write_json_atomic(args.output, build_report(cases))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"quality-eval: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
