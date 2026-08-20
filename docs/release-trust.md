# Future release trust path

This document applies to future Coreguard tags only. Coreguard `v0.1.0` was
published before the attesting workflow existed; it remains an unsigned,
SHA-256-pinned historical release and is not retroactively represented as
having build provenance.

## Selected architecture

The minimum selected path is:

1. Build and test the exact `vX.Y.Z` tag on Windows x64/MSVC Release.
2. Fail closed on tag/version, CMake/CPack filename, and package-content drift.
3. Generate `SHA256SUMS` for the release ZIP and a `release-manifest.json` with
   the ZIP and embedded `bin/coreguard.exe` digests.
4. Generate a GitHub Artifact Attestation for the ZIP, `SHA256SUMS`, and
   `release-manifest.json`.
5. Upload the evidence as a workflow artifact. GitHub Release publication is
   available only through manual dispatch with the protected
   `release-publication` environment.

The release unit is the ZIP because that is what consumers download. The
embedded EXE is covered by the manifest and package digest; it is not
attested as a misleading standalone release artifact.

The workflow is `.github/workflows/release.yml`. It has no pull-request trigger,
does not create tags, and a tag push does not publish a GitHub Release.

## Consumer verification

After downloading a future release ZIP and the two evidence files:

```powershell
Get-FileHash .\coreguard-X.Y.Z-windows-x64-msvc.zip -Algorithm SHA256
Get-Content .\SHA256SUMS
Expand-Archive .\coreguard-X.Y.Z-windows-x64-msvc.zip -DestinationPath .\coreguard-X.Y.Z
Get-FileHash .\coreguard-X.Y.Z\bin\coreguard.exe -Algorithm SHA256
gh attestation verify .\coreguard-X.Y.Z-windows-x64-msvc.zip `
  --repo trungminhdo4-glitch/coreguard `
  --signer-workflow trungminhdo4-glitch/coreguard/.github/workflows/release.yml `
  --source-ref refs/tags/vX.Y.Z
```

The ZIP digest must match `SHA256SUMS` and `zip_sha256` in
`release-manifest.json`. The embedded executable digest must match
`coreguard_exe_sha256`. A changed ZIP must fail both the SHA-256 check and
attestation verification. Attestation establishes a cryptographic link to the
repository, source ref/commit, workflow, and artifact digest; it does not
prove that the source is vulnerability-free or that the program is safe for
every use.

## Deferred decisions

- `SBOM_DEFERRED_LOW_INCREMENTAL_VALUE`: Coreguard currently exposes a very
  small native dependency surface and does not vendor third-party runtime
  packages. The Windows SDK/MSVC toolchain is build environment metadata, not a
  dependency shipped in the ZIP. A future dependency-bearing release can add
  an SPDX or CycloneDX SBOM and an SBOM attestation using the same GitHub
  mechanism.
- `AUTHENTICODE_NEXT_LATER`: No certificate, Azure account, signing secret, or
  signing workflow is introduced in this wave. Microsoft Artifact Signing is
  the likely next evaluation for public non-Store distribution; traditional OV
  remains a fallback where identity or geography prevents that route. Signing
  would protect the PE publisher/integrity and improve Windows trust signals,
  but it is distinct from GitHub build provenance.
- `PACKAGE_MANAGER_DEFERRED_UNTIL_RELEASE_TRUST`: WinGet requires a stable,
  version-specific installer URL and an installer SHA-256 manifest. Coreguard
  currently ships a portable ZIP/EXE contract without an installer, so no
  WinGet manifest is added until signing and release publication are settled.

## Research anchors

The implementation follows the current official guidance for [GitHub Artifact
Attestations](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations),
[`gh attestation verify`](https://cli.github.com/manual/gh_attestation_verify),
[SLSA Provenance v1](https://slsa.dev/spec/v1.0/provenance),
[Sigstore blob verification](https://docs.sigstore.dev/cosign/verifying/verify/),
[Microsoft SignTool](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool),
[Microsoft SmartScreen reputation](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation),
[Microsoft Artifact Signing](https://learn.microsoft.com/en-us/azure/artifact-signing/overview),
and [WinGet manifests](https://learn.microsoft.com/en-us/windows/package-manager/package/manifest).
