# Integration evidence — 2026-10-02

The tested main source is `8970375fc3cc490fe0a23d321255a0e28594e2d9`.

- An independent Linux clone passed the complete 242-test component suite, with no skips.
- Six real Docker/VerificationBroker cases reached their expected outcomes: normal tool execution, missing axe, failed application startup, API-only N/A, injected overflow, and injected axe violations. Each produced one continuation, no UNKNOWN fence, verified cleanup, and host-owned private artifacts.
- The normal toy application produced genuine automated findings (`landmark-one-main` and `page-has-heading-one`). Its UIQA criteria remain FAILED. Tool-path validation does not certify application quality or accessibility conformance. Screenshots were hashed, not visually reviewed.
- Forced SIGKILL cleanup and ownership return remain PARTIAL. The existing cancellation path must not be treated as proof that external Docker effects were cleaned up.

Earlier real Band collaboration completed toy stages 1–4 across factory revisions `5ce0bd3`, `d82b8f1`, and `97bd97b`, with development repairs. That evidence is distinct from a fresh native qualification of this main revision and from real tablekeeper qualification.

The local `adapter-boundary` candidate `5781412d741997dbbb3ac3b49171c9da7ed8bb4b` passed 202 component tests, including import isolation, synthetic backend lifecycle, and preserved Codex proof bytes. It has **not been merged**: fresh-room F1 readiness is NOT_RUN. Main still has the shared Codex dependencies documented in the work-order findings; registering another backend does not establish generic recovery support. Claude account authentication, inference, and mixed-seat live qualification remain NOT_RUN.

A new development room is still required for F1/F3 readiness and the tablekeeper stage-1 rehearsal. The completed toy run is not restarted or reused. The submission repository has not been created, and no final submission or tablekeeper stage 2–4 execution is claimed.
