from __future__ import annotations

import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW_ROOT = ROOT / ".github" / "workflows"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def workflow_text(name: str) -> str:
    return (WORKFLOW_ROOT / name).read_text(encoding="utf-8")


def workflow_files() -> list[pathlib.Path]:
    return sorted(
        [*WORKFLOW_ROOT.glob("*.yml"), *WORKFLOW_ROOT.glob("*.yaml")],
        key=lambda path: path.name,
    )


def action_refs(text: str) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = re.match(r"^\s*uses:\s*(\S+)", line)
        if not match:
            continue
        reference = match.group(1).split("#", 1)[0]
        if reference.startswith("./"):
            continue
        if "@" not in reference:
            refs.append((reference, "missing @ ref"))
            continue
        action, ref = reference.rsplit("@", 1)
        refs.append((action, ref))
    return refs


def permission_blocks(text: str) -> list[tuple[str, ...]]:
    blocks: list[tuple[str, ...]] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "permissions:":
            continue
        base_indent = len(line) - len(line.lstrip())
        entries: list[str] = []
        for candidate in lines[index + 1 :]:
            if not candidate.strip():
                break
            candidate_indent = len(candidate) - len(candidate.lstrip())
            if candidate_indent <= base_indent:
                break
            entries.append(candidate.strip())
        blocks.append(tuple(entries))
    return blocks


class WorkflowSecurityTests(unittest.TestCase):
    def test_all_workflow_action_refs_are_full_commit_shas(self) -> None:
        for path in workflow_files():
            for action, ref in action_refs(path.read_text(encoding="utf-8")):
                self.assertRegex(
                    ref,
                    SHA_PATTERN,
                    f"{path.name}: {action}@{ref} is not an immutable full SHA",
                )

    def test_pin_guard_rejects_floating_refs(self) -> None:
        for reference in (
            "actions/checkout@main",
            "actions/checkout@master",
            "actions/checkout@v4",
            "actions/checkout@v4.2.1",
        ):
            action, ref = reference.rsplit("@", 1)
            self.assertFalse(
                SHA_PATTERN.fullmatch(ref),
                f"test fixture unexpectedly looks immutable: {action}@{ref}",
            )

    def test_pr_validation_is_read_only_and_non_publishing(self) -> None:
        workflow = workflow_text("release-trust-ci.yml")
        self.assertIn("pull_request:", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertEqual(permission_blocks(workflow), [("contents: read",)])
        for forbidden in (
            "attestations: write",
            "id-token: write",
            "contents: write",
            "gh release create",
            "release-publication",
            "git tag",
        ):
            self.assertNotIn(forbidden, workflow)
        self.assertIn("runs-on: windows-latest", workflow)
        self.assertIn("scripts/windows_release_trust.ps1", workflow)
        self.assertIn("actions/upload-artifact@", workflow)
        self.assertNotIn("actions/attest@", workflow)

    def test_release_workflow_has_no_pr_trigger_and_minimal_job_permissions(self) -> None:
        workflow = workflow_text("release.yml")
        self.assertNotIn("pull_request:", workflow)
        self.assertEqual(
            permission_blocks(workflow),
            [
                ("contents: read",),
                ("contents: read", "id-token: write", "attestations: write"),
                ("contents: write",),
            ],
        )
        self.assertIn("if: ${{ github.event_name == 'workflow_dispatch' && inputs.publish == true }}", workflow)
        self.assertIn("environment:\n      name: release-publication", workflow)
        self.assertIn("needs: attest", workflow)

    def test_package_and_manifest_gates_precede_attestation_and_publication(self) -> None:
        workflow = workflow_text("release.yml")
        build_step = workflow.index("Build, test, package, and verify release trust evidence")
        attest_step = workflow.index("uses: actions/attest@")
        publish_job = workflow.index("\n  publish:\n")
        self.assertLess(build_step, attest_step)
        self.assertLess(attest_step, publish_job)

        shared = (ROOT / "scripts" / "windows_release_trust.ps1").read_text(encoding="utf-8")
        gate_names = (
            "validate-version",
            "validate-build",
            "validate-package",
            "write-evidence",
            "verify-sha256sums",
            "verify-manifest",
        )
        positions = [shared.index(name) for name in gate_names]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("cpack --config", shared)
        self.assertIn("scripts\\release_trust.py", shared)

    def test_workflows_are_parseable_yaml_when_parser_is_available(self) -> None:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            self.skipTest("PyYAML is not installed; GitHub Actions parses YAML remotely")
        for path in workflow_files():
            with self.subTest(workflow=path.name):
                self.assertIsInstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict)


if __name__ == "__main__":
    unittest.main()
