# Third-party notices

The credential-shape regular expressions in `tools/public_guard.py` derive from
`harness/check.py` in Band's Dark Factory repository at commit
`803560d2a678ace1414465c098eb0ab5380ffade`:

https://github.com/band-ai/dark-factory-wearedevs/blob/803560d2a678ace1414465c098eb0ab5380ffade/harness/check.py

That source is licensed under Apache License 2.0. Its license is preserved in
`licenses/Apache-2.0.txt`. DarkHarness implements a separate index and history
scanner around those shapes and adds its own private-file and identity checks.
The contest checker, track specifications and test suites are not vendored here.

Band SDK is an external dependency, not bundled source. Runtime integration
subclasses and wraps its pinned interfaces; dependencies retain their own licenses.
Original DarkHarness code is covered by the repository's MIT LICENSE.
