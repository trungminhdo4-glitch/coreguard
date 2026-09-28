"""Re-seal the frozen public ABI contract.

This is the review ceremony for a deliberate public-surface change: it
recompiles the manifest, shows the difference against the current contract,
and writes a newly sealed contract. It never runs in CI; ``test_abi_freeze``
only verifies the result.

    python tests/abi/freeze_abi_contract.py --reason "why the contract changes"
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

ABI_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ABI_DIR))

import abi_gate  # noqa: E402


def default_source() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=abi_gate.ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if completed.returncode != 0:
        return "unknown"
    return completed.stdout.strip()


def summarize(previous: dict[str, str], current: dict[str, str]) -> list[str]:
    lines: list[str] = []
    for key in sorted(set(previous) - set(current)):
        lines.append(f"REMOVED {key} (was {previous[key]!r})")
    for key in sorted(set(previous) & set(current)):
        if previous[key] != current[key]:
            lines.append(f"CHANGED {key}: {previous[key]!r} -> {current[key]!r}")
    for key in sorted(set(current) - set(previous)):
        lines.append(f"ADDED   {key}={current[key]!r}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reason", required=True, help="why the contract is re-sealed")
    parser.add_argument(
        "--source",
        default=None,
        help="frozen source description (default: current HEAD)",
    )
    parser.add_argument(
        "--contract",
        type=pathlib.Path,
        default=abi_gate.CONTRACT_PATH,
    )
    args = parser.parse_args(argv)

    if not args.reason.strip():
        parser.error("--reason must not be empty")
    if not abi_gate.vcvars_available():
        print(
            "missing vcvars64.bat; set COREGUARD_VCVARS or add it to PATH",
            file=sys.stderr,
        )
        return 2

    entries, provenance, _ = abi_gate.collect_manifest()

    allowlist: list[str] = []
    previous_entries: dict[str, str] = {}
    if args.contract.is_file():
        previous, _ = abi_gate.load_contract(args.contract)
        raw_allowlist = previous.get("additive_allowlist", [])
        if isinstance(raw_allowlist, list):
            allowlist = [str(entry) for entry in raw_allowlist]
        raw_entries = previous.get("entries", {})
        if isinstance(raw_entries, dict):
            previous_entries = {
                str(key): str(value) for key, value in raw_entries.items()
            }

    body = abi_gate.contract_body(
        entries,
        allowlist,
        args.source or default_source(),
    )
    body["seal_sha256"] = abi_gate.compute_seal(body)

    changes = summarize(previous_entries, entries)
    if changes:
        print("contract changes:")
        for line in changes:
            print(f"  {line}")
    else:
        print("contract entries unchanged")
    print(f"reason: {args.reason.strip()}")
    print(
        "observed with: "
        + ", ".join(f"{key}={value}" for key, value in sorted(provenance.items()))
    )

    args.contract.parent.mkdir(parents=True, exist_ok=True)
    args.contract.write_text(
        json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.contract}")
    print(f"seal_sha256={body['seal_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
