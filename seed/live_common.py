"""Shared helpers for pushing the demo scenario to real services."""
from __future__ import annotations

import json
from pathlib import Path

from teampulse.config import ROOT

RUNS = ROOT / "data" / "seed_runs"


def parse_map(text: str | None) -> dict[str, str]:
    """'siddhi=Siddhi-Karhekar,aarav=friend1' → {'siddhi': 'Siddhi-Karhekar', 'aarav': 'friend1'}"""
    out = {}
    for part in (text or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def save_run(name: str, data: dict) -> Path:
    RUNS.mkdir(parents=True, exist_ok=True)
    path = RUNS / f"{name}.json"
    old = json.loads(path.read_text()) if path.exists() else {}
    old.update(data)
    path.write_text(json.dumps(old, indent=2, ensure_ascii=False))
    return path


def load_run(name: str) -> dict:
    path = RUNS / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else {}
