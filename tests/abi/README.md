# Frozen public ABI contract

`contract_v0.2.0.json` is the sealed observation of the complete public header
surface of the v0.2.0 release candidate: struct sizes, alignments, and field
offsets, `cg_status` values, resource-limit and metric constants, and the
typed public function signatures.

`tests/test_abi_freeze.py` recompiles `tests/consumers/layout_manifest/main.c`
and `tests/consumers/abi_signature/main.c` with MSVC and compares the
observation against the sealed file:

- a changed or removed entry fails the gate,
- an added entry fails unless it is listed in `additive_allowlist`,
- a stale allowlist entry fails,
- an edit that is not re-sealed fails the digest check.

The gate cannot prevent a commit from doing header change plus re-freeze in one
step. Its purpose is to make that step deliberate and visible: the contract
file must be touched and re-sealed, and its diff is the review signal.

Re-seal after a reviewed public-surface change:

```powershell
python tests/abi/freeze_abi_contract.py --reason "why the contract changes"
```
