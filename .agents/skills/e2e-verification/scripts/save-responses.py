#!/usr/bin/env python3
"""Save exact AI-Q report bodies and a readable question-and-response record."""

import argparse
import json
from pathlib import Path


def save_responses(directory: Path) -> None:
    question = (directory / "question.txt").read_text(encoding="utf-8")
    sections = ["# E2E verification responses\n\n## Question\n\n" + question]
    found = False
    for label in ("shallow", "deep"):
        report_path = directory / f"{label}-report.json"
        if not report_path.exists():
            continue
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not payload.get("has_report"):
            raise ValueError(f"{report_path}: no final agent report")
        response = payload.get("report")
        if not isinstance(response, str) or not response.strip():
            raise ValueError(f"{report_path}: final response is empty or is not text")
        job_id = payload.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError(f"{report_path}: missing job ID")
        (directory / f"{label}-response.md").write_text(response, encoding="utf-8")
        sections.append(
            f"## {label.capitalize()} researcher (`{label}_researcher`)\n\n"
            f"Job ID: `{job_id}`\n\n{response}"
        )
        found = True
    if not found:
        raise ValueError(f"{directory}: no agent reports found")
    (directory / "responses.md").write_text("\n\n".join(sections) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_dir", type=Path)
    args = parser.parse_args()
    try:
        save_responses(args.artifact_dir)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Cannot save agent responses: {exc}\n")


if __name__ == "__main__":
    main()
