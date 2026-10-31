"""Fail if an API key or token was ever committed: every tracked file, and every change in the git history.

    python scripts/scan_secrets.py            # working tree + full history
    python scripts/scan_secrets.py --tree     # working tree only (fast)

Uses the same patterns TeamPulse removes from AI requests at runtime (teampulse/guardrails.py SECRET), plus private
keys. Test fixtures use obvious placeholders (gsk_AAAA…, gsk_abcdefgh…); those are skipped. Anything
else is reported with file/commit and line, with the secret itself masked. If this ever fires on a real key: revoke the
key first (that's what makes it safe), then clean it from history.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from teampulse.guardrails import SECRET  # noqa: E402

PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
SKIP_FILES = {"requirements.lock"}


def placeholder(secret: str) -> bool:
    """Obvious test values: one or two characters repeated (gsk_AAAA…), or the alphabet in order (gsk_abcdef…)."""
    body = secret.split("_", 1)[-1] if "_" in secret[:12] else secret[4:]
    return len(set(body)) <= 2 or body.lower().startswith("abcdefgh")


def mask(secret: str) -> str:
    return secret[:6] + "…" + f"({len(secret)} chars)"


def findings_in(text: str) -> list[tuple[int, str]]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in SECRET.finditer(line):
            if not placeholder(m.group(0)):
                out.append((n, mask(m.group(0))))
        if PRIVATE_KEY.search(line):
            out.append((n, "private key"))
    return out


def scan_tree() -> list[str]:
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    hits = []
    for name in files:
        if name in SKIP_FILES:
            continue
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        hits += [f"{name}:{n}: {what}" for n, what in findings_in(text)]
    return hits


def scan_history() -> list[str]:
    log = subprocess.run(["git", "log", "-p", "--all", "--no-color", "--format=COMMIT %H"], cwd=ROOT,
                         capture_output=True, text=True, check=True, encoding="utf-8", errors="replace").stdout
    hits, commit, path = [], "?", "?"
    for line in log.splitlines():
        if line.startswith("COMMIT "):
            commit = line[7:15]
        elif line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("+") and not line.startswith("+++") and path not in SKIP_FILES:
            hits += [f"commit {commit} {path}: {what}" for _, what in findings_in(line[1:])]
    return sorted(set(hits))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tree", action="store_true", help="only the current files")
    args = parser.parse_args()
    hits = scan_tree() + ([] if args.tree else scan_history())
    if hits:
        print("Possible secrets found (masked):")
        print("\n".join(f"  {h}" for h in hits))
        return 1
    print("No secrets found in the tracked files" + ("" if args.tree else " or the git history") + ".")
    return 0


if __name__ == "__main__":
    sys.exit(main())
