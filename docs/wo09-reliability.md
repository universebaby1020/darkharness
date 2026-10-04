# Reliability candidate: native error schema and bounded pre-start retries

This candidate corrects two native tagged error representations and changes
proven pre-start failures to three retries (four attempts total), with the
existing 60-second delay. Both ordinary release and owner restart preserve the
same durable counter. The fourth failure releases the queue through the existing
terminal path. No accepted turn or uncertain effect is reclassified as unstarted.

Provider recovery still requires the existing authenticated dispatch binding,
pre-registered authority, unchanged source/settings, native terminal evidence,
zero observed effects and proved process cessation. Safety refusals and stream
failure variants remain outside its allowlist. No safety-refusal retry or new
thread path is introduced.

The mandate adds two sentences requesting precise data-validation descriptions
and explicitly preserving test scope and literal evidence quotations. These do
not establish why an earlier refusal occurred or whether wording will change
future model behavior. No live filter experiment has been performed.

The follow-up candidate accepts preceding retryable native error frames only
for the same client, thread and turn, before the single definitive error frame
matching the completed turn. This applies to the existing zero-effect provider
path; the explicit capacity action keeps its original single-frame contract.
Repeated blocker notifications retain the same child but now emit their durable
event only on the first insert. A restart reuses this identity. Multiple harmless
wake requests must not be confused with multiple blocker works or events.

Operator observation/shutdown helpers are separately versioned operational
artifacts, not installed by this product commit. They require their own evidence
and future readiness before use. This candidate alone does not qualify a new
submission run. Main integration, publication and new-run dispatch remain pending.
