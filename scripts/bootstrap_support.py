#!/usr/bin/env python3
"""Small stdlib-only helpers used by the first-run bootstrap."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def write_env_if_missing(template: Path, destination: Path, values: dict[str, str]) -> bool:
    if destination.exists():
        print(f"SETUP_STAGE:env:Preserved existing {destination}")
        return False
    text = template.read_text()
    lines = []
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _ = line.split("=", 1)
            if key in values:
                line = f"{key}={json.dumps(values[key])}"
        lines.append(line)
    payload = "\n".join(lines) + "\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(payload)
    print(f"SETUP_STAGE:env:Created {destination} from .env.example")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-env-if-missing", action="store_true")
    parser.add_argument("--template", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--values-json", type=Path)
    args = parser.parse_args()
    if args.write_env_if_missing:
        if not all((args.template, args.destination, args.values_json)):
            parser.error("--template, --destination, and --values-json are required")
        values = json.loads(args.values_json.read_text())
        write_env_if_missing(args.template, args.destination, values)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
