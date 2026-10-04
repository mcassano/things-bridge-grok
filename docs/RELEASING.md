# Releasing

Releases are cut from `main` with a signed, annotated tag. The Release workflow
verifies the tag signature with GitHub, reruns the checks, builds source
archives with `SHA256SUMS`, and publishes them.

1. Update the version in all three places:
   - `HELPER_VERSION` in `bin/helper.py`
   - `version:` in the `SKILL.md` frontmatter
   - a new `## X.Y.Z - YYYY-MM-DD` heading in `CHANGELOG.md`
2. If the protocol changed, bump `PROTOCOL_VERSION` (minor for additions,
   major for breaking changes) and update `docs/PROTOCOL.md`.
3. Merge to `main` with CI green.
4. Make sure tag signing is configured (`git config tag.gpgsign true`, with a
   GPG or SSH signing key that GitHub can verify).
5. Run `tools/cut-release.sh vX.Y.Z`. It runs the tests, checks versions,
   requires a clean tree, and creates the signed tag locally.
6. Review the tag, then `git push origin vX.Y.Z`.

Never move or recreate a published tag; release a new patch version instead.
