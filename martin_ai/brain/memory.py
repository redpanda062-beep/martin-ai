from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import BrainState
from martin_ai.response_formatter import investigation_assessment


REPORT_DIR = Path.home() / "Documents" / "MartinAI Investigations"


def _safe_name(text: str) -> str:
    value = "".join(char if char.isalnum() else "-" for char in text.lower())
    return "-".join(part for part in value.split("-") if part)[:48] or "investigation"


def save_report(state: BrainState) -> dict[str, str]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now():%Y%m%d-%H%M%S}-{_safe_name(state.user_request)}"
    json_path = REPORT_DIR / f"{stem}.json"
    md_path = REPORT_DIR / f"{stem}.md"
    data = state.to_dict()
    json_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    evidence = "\n".join(
        f"- **{item.get('tool', 'tool')}**: `{json.dumps(item.get('result'), default=str)}`"
        for item in state.evidence
    ) or "- No usable evidence was collected."
    recommendations = "\n".join(f"- {item}" for item in state.recommendations) or "- None."
    errors = "\n".join(f"- {item}" for item in state.errors) or "- None."
    history = "\n".join(
        f"- {item.get('problem', '')}: " +
        (f"{item.get('repair_id', 'repair')} — " + ("verified after repair" if item.get('verified') else "not verified as a fix")
         if item.get('kind') == 'repair' else str(item.get('diagnosis', 'Prior investigation recorded.')))
        for item in state.related_history
    ) or "- No matching history."
    markdown = (
        f"# Martin AI Investigation\n\n"
        f"Started: {state.started_at}  \nUpdated: {state.updated_at}\n\n"
        f"## Request\n\n{state.user_request}\n\n"
        f"## Diagnosis\n\n{state.diagnosis or 'Inconclusive'}\n\n"
        f"**Confidence:** {state.confidence}\n\n"
        f"{investigation_assessment(state)}\n\n"
        f"## Evidence\n\n{evidence}\n\n"
        f"## Recommendations\n\n{recommendations}\n\n"
        f"## Similar past cases\n\nHistorical context only; fresh checks are required.\n\n{history}\n\n"
        f"## Errors or unavailable sensors\n\n{errors}\n"
    )
    md_path.write_text(markdown, encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}


def recent_history(limit: int = 3) -> list[dict[str, Any]]:
    if not REPORT_DIR.exists():
        return []
    results = []
    for path in sorted(REPORT_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            results.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return results
