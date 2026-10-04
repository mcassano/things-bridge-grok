# Signing and Notarization

This project publishes source archives and checksums, not prebuilt binaries.
Each installer compiles the wrapper and the confirmation dialog with paths
specific to your Mac, then signs them locally:

- By default both installers use an **ad hoc** signature with the hardened
  runtime. That gives macOS a stable local code identity (CDHash) for the Full
  Disk Access grant and blocks dynamic-library injection, but it does not
  establish publisher identity.
- `CODESIGN_IDENTITY="Developer ID Application: …" ./install-hardened.sh`
  signs with your own Developer ID and a secure timestamp instead.

The resulting binaries are **not notarized**. Notarization is Apple's review
of a distributable artifact; a locally compiled, path-specific binary is not
one. `codesign --verify` (run by `tools/doctor.py`) checks structural validity
only.

To establish trust in what you run: review the source, verify the release
archive against `SHA256SUMS`, confirm the tag signature on GitHub, and prefer
the hardened install when its trust model fits your Mac.
