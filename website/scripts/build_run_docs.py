"""Bundle allowlisted runner documentation for the authenticated website."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path


WEBSITE = Path(__file__).resolve().parents[1]
ROOT = WEBSITE.parent
OUTPUT = WEBSITE / "content/run-repo-files.json"
FILES = (
    "docs/DRIVER_CONTRACT.md",
    "examples/configs/hermes-builtin.yaml",
    "examples/configs/offline.yaml",
    "examples/harness_template.py",
    "examples/mcp_connection.py",
    "examples/offline_adapter.py",
    "examples/reference/README.md",
    "graders/mechanical.py",
    "harness/submission.py",
)
VALIDATOR_FILES = (
    'harness/__init__.py', 'harness/submission.py', 'harness/costing.py', 'harness/environment.py',
    'harness/dataset.py',
    'graders/__init__.py', 'graders/mechanical.py', 'graders/explicit.py',
    'graders/llm_judge.py', 'graders/judge_recording.py',
    'graders/markdown_bullets.py',
)


def build() -> dict:
    return {
        name: {
            "content": (ROOT / name).read_text(),
            "sha256": hashlib.sha256((ROOT / name).read_bytes()).hexdigest(),
        }
        for name in FILES
    }


def build_validator(root: Path = ROOT, destination: Path | None = None) -> dict:
    """Package trusted data and unchanged grader code for the Python function."""
    sys.path.insert(0, str(root))
    from harness.submission import read_release
    destination = destination or WEBSITE / '.validation'
    releases = read_release(root)
    # Validation replays recorded actions against checks; it never launches apps.
    # Exclude app snapshots from the serverless function's data bundle.
    payload = {persona: {
        'sessions': release['sessions'],
        'tests': [{key: test[key] for key in ('id', 'test', 'narrative_anchor_date', 'grade')}
                  for test in release['tests']],
    } for persona, release in releases.items()}
    destination.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    (destination / 'release.json').write_bytes(raw)
    hashes = {}
    for relative in VALIDATOR_FILES:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)
        hashes[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    shutil.copyfile(root / 'pricing/latest.json', destination / 'pricing.json')
    hashes['pricing.json'] = hashlib.sha256((destination / 'pricing.json').read_bytes()).hexdigest()
    info = {
        'release_sha256': hashlib.sha256((root / 'manifest.json').read_bytes()).hexdigest(),
        'release_json_sha256': hashlib.sha256(raw).hexdigest(),
        'validator_sha256': hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
        'files': hashes,
    }
    (destination / 'build.json').write_text(json.dumps(info))
    return info


if __name__ == "__main__":
    if '--validator' in sys.argv:
        build_validator()
        print('Bundled submission validator and verified release')
    else:
        OUTPUT.write_text(json.dumps(build(), ensure_ascii=False, indent=2) + "\n")
        print(f"Bundled {len(FILES)} runner files")
