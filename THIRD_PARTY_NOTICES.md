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

## Selected UI design pack

The text-only pack is at `docs/design/ui-design-engineering-clean/`, outside the Python package. Its local notices and five byte-preserved license files stay in place. Source names are attribution, not endorsement. No images, logos, fonts, npm packages, axe-core assets, or browser binaries are bundled.

| Owner/repository | Pinned commit | Used upstream paths | Copyright/attribution | License | Local license copy |
|---|---|---|---|---|---|
| [ emilkowalski/skills ](https://github.com/emilkowalski/skills) | `85e8e2363b713506e1d5b6e07a0eb2da66be1bc3` | `skills/emil-design-eng/SKILL.md` | 2026 Emil Kowalski | MIT | `docs/design/ui-design-engineering-clean/licenses/emil-design-eng-LICENSE` |
| [ jakubkrehel/make-interfaces-feel-better ](https://github.com/jakubkrehel/make-interfaces-feel-better) | `35545ea1512ad59fa463e6b1f95ca9c052981fe6` | `skills/make-interfaces-feel-better/*` | 2026 Jakub Krehel | MIT | `docs/design/ui-design-engineering-clean/licenses/make-interfaces-feel-better-LICENSE` |
| [ ibelick/ui-skills ](https://github.com/ibelick/ui-skills) | `b1cc8e0073ac64b09b3d38cd604407aa20c2b7ad` | `skills/fixing-accessibility/SKILL.md` | 2026 Julien Thibeaut | MIT | `docs/design/ui-design-engineering-clean/licenses/fixing-accessibility-LICENSE` |
| [ microsoft/playwright-cli ](https://github.com/microsoft/playwright-cli) | `74354ecc7a43da16d91a9bc54fa8db8283a3fcf5` | `skills/playwright-cli/*` | Microsoft Corporation (upstream source attribution) | Apache-2.0 | `docs/design/ui-design-engineering-clean/licenses/playwright-cli-LICENSE` |
| [ shadcn-ui/ui ](https://github.com/shadcn-ui/ui) | `98a1fe67b439324ddc857f47fbdce056600a4329` | `skills/shadcn/*` | 2023 shadcn | MIT | `docs/design/ui-design-engineering-clean/licenses/shadcn-LICENSE.md` |

`references/browser-verification.md` is an Apache-2.0 derivative (Korean adaptation and authority, evidence, source and version-notice edits dated 2026-10-02). The supplied 2026-10-02 pinned-tree inspection found no upstream NOTICE in these five sources. Other retained derivative portions keep their respective upstream licenses, not the root MIT grant. FILE_SOURCE_MAP.json records upstream paths at the granularity supported by the adopted work order; directory wildcards are not a sentence-level provenance audit.

C7=1: Original authored wording in `SKILL.md, governance/*, references/motion.md, references/layout-and-state.md, SOURCES_AND_SCOPE.md` inside the pack follows this repository's MIT LICENSE. This grant does not relicense retained third-party portions. Original DarkHarness code follows the root MIT LICENSE. No legal certainty or future asset clearance is claimed.
