# Release checklist

- [ ] Bump the version in applicable project metadata/docs; keep the release tag and notes aligned.
- [ ] Run the full battery from a clean working tree: `python -X utf8 -m tests.test_smoke`, `python -X utf8 -m tests.test_scenarios`, `python -X utf8 -m pytest`, `ruff check .`, and `PYTHONPYCACHEPREFIX=.test-artifacts/pycache python -X utf8 -m compileall -q .`.
- [ ] Clone `origin` into a clean directory and repeat the release checks from that clone.
- [ ] On Windows, run `scripts/build_windows.ps1`; record the executable size and confirm it is within the release budget.
- [ ] Run `scripts/verify_exe.ps1` and confirm it reports `PASS` for the frozen executable.
- [ ] Run a real-bot end-to-end check against the frozen executable; confirm boot and representative interactions, with no live Discord transport.
- [ ] Tag `vX.Y.Z-rc1` and push the tag. If anything changes, rerun the affected checks and repeat the RC cycle with a new candidate tag.
- [ ] After the candidate is accepted, tag `vX.Y.Z` and push the final tag.
- [ ] Draft release notes from `v<prev>..vX.Y.Z` (`git log --oneline` and `git diff --stat`); include the actual changes, honest known limitations, and install/quick-start links. Publish the same notes with the GitHub Release.
