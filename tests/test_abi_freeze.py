"""Frozen public ABI contract gate for the v0.2.0 release candidate.

The in-header static assertions protect the layouts the header itself knows
about, but they can be updated in the same commit that changes a layout, and
they do not cover every field, enum, macro, or function signature. This gate
compares the complete observed public surface against a sealed contract that
lives in a separate file, so any drift must touch that file deliberately.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ABI_DIR = pathlib.Path(__file__).resolve().parents[0] / "abi"
sys.path.insert(0, str(ABI_DIR))

import abi_gate  # noqa: E402

REFREEZE_COMMAND = (
    'python tests/abi/freeze_abi_contract.py --reason "<why the contract changes>"'
)


class PublicAbiFreezeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not abi_gate.vcvars_available():
            raise unittest.SkipTest(
                "missing vcvars64.bat; set COREGUARD_VCVARS or add it to PATH"
            )
        cls.collection_error: str | None = None
        cls.entries: dict[str, str] = {}
        cls.provenance: dict[str, str] = {}
        try:
            cls.entries, cls.provenance, cls.manifest_log = abi_gate.collect_manifest()
        except abi_gate.AbiGateError as exc:
            cls.collection_error = str(exc)

    def require_manifest(self) -> None:
        if self.collection_error is not None:
            self.fail(self.collection_error)

    def test_signature_surface_compiles(self) -> None:
        try:
            abi_gate.compile_signature()
        except abi_gate.AbiGateError as exc:
            self.fail(str(exc))

    def test_frozen_contract_is_sealed(self) -> None:
        contract, recorded_seal = abi_gate.load_contract()
        if recorded_seal is None:
            self.fail(
                "frozen contract has no seal_sha256; re-freeze it with:\n"
                f"{REFREEZE_COMMAND}"
            )
        computed = abi_gate.compute_seal(contract)
        self.assertEqual(
            computed,
            recorded_seal,
            "frozen contract was edited without re-sealing; re-freeze it with:\n"
            f"{REFREEZE_COMMAND}",
        )

    def test_contract_matches_current_header(self) -> None:
        self.require_manifest()
        contract, _ = abi_gate.load_contract()
        problems = abi_gate.diff_contract(self.entries, contract)
        if problems:
            provenance = ", ".join(
                f"{key}={value}" for key, value in sorted(self.provenance.items())
            )
            self.fail(
                "public ABI drifted from the frozen v0.2.0 contract:\n"
                + "\n".join(f"- {problem}" for problem in problems)
                + f"\nobserved with: {provenance}"
                + f"\nre-freeze only after review: {REFREEZE_COMMAND}"
            )

    def test_frozen_contract_shape(self) -> None:
        contract, _ = abi_gate.load_contract()
        self.assertEqual(contract.get("schema"), abi_gate.CONTRACT_SCHEMA)
        self.assertEqual(contract.get("target"), abi_gate.CONTRACT_TARGET)
        source = contract.get("source")
        self.assertIsInstance(source, str)
        self.assertTrue(source)
        entries = contract.get("entries")
        self.assertIsInstance(entries, dict)
        self.assertGreaterEqual(len(entries), 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
