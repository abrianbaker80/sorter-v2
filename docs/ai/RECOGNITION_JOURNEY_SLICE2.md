# Slice 2: Recognition Journey

Status: COMPLETE for local implementation and validation of the isolated
recognition contract. Not deployed or connected to a physical runtime. Slice 1
is unchanged and frozen. Slice 3 has not begun.

## Identity and caller contract

`RecognitionJourneys.start_c3(piece, episode_id, generation, scene, alias)`
accepts the original `KnownObject` at C3 selection, before release. Its existing
UUID is the only piece identity. A journey retains that object, UUID, transfer
reference, generation, observation aliases, copied images, frozen request and
identity-scoped result. It does not create another UUID or mutate the object's
classification or physical transport fields.

Aliases are `(channel, camera+tracker epoch, numeric track ID, track generation)`.
The existing `tracked_global_id` remains an optional history locator paired
with history `created_at`; neither field establishes physical ownership.

The local API is deliberately callable without a motor, controller, pocket or
FIFO. The future integration must supply complete paired `Scene` publications,
notify lifecycle epochs using `set_epoch`, increment generations when numeric
IDs are reused, and supply original same-frame alias bridges when available.
These are evidence requirements, not guesses manufactured from timestamps.
The current production controller/collectors were not wired to this new API in
Slice 2. Runtime wiring and physical-generation binding require later review.

## Reused captures and association

| Stage | Original mechanism reused | Eligibility |
|---|---|---|
| C3 ready | Exact paired selected-piece frame | Generation-scoped alias, isolated bbox |
| C3 history | `capture_release_view` and `ChannelCropCollector.ready_c3_views` | Exact release anchor plus original paired epoch/generation scene for every cached crop; copied pixels must match the collector's padded crop |
| C3 exit | Same original C3 paired frame/crop mechanism at `begin_crossing` | Selected leader, correct alias, exclusive departure context |
| Fall/tumble | `DropZoneBurstCollector.rolling_buffer` / `RollingFrameBuffer.snapshot`, and `drop_zone_burst` history format | Frames after this release and before landing, original complete detections, matching source lifecycle; landing alias or explicit same-frame alias bridge |
| C4 landing/bounce | Isolated paired C4 detection; original post-burst history | Proven C4 alias; only this piece in its crop |
| C4 settled | Original `carousel` sector snapshots and paired C4 views | Proven alias/generation and original paired frame |

The legacy burst's `detected=True` and a nearby timestamp are insufficient.
The adapter omits such records if original complete-scene provenance is absent.
No original detector, capture cadence, crop thresholds or provider encoding was
rewritten. No extra inference, model association, disk wait or live lookup is
performed to make optional evidence available.

Without physical FIFO context, the supported C3-to-C4 proof is intentionally
conservative: a complete singleton C3 release, a fresh globally empty C4
baseline, one active crossing, complete C3 absence afterward and one arriving
C4 detection. A second piece anywhere contradicts exclusivity. Crowded scenes
therefore do not qualify for this upstream association. That never changes
sorting state; an independently valid C4-only journey still submits its one
available view. This slice does not claim reliable loaded-multipiece association.

Track changes in C4 require both old and new labels on the same original frame
and exact same detection bbox. Nearby or later-only labels are omitted. Fall
aliases must be connected to the landing alias through that same explicit proof.
Previous/follower aliases, reused IDs, old generations, camera restarts, stale
fall frames, incomplete detections and overlapping crossings cannot grant access
to earlier images. Invalid optional data returns omission, not a control event.
Track loss does not erase already-owned image copies.

## Selection and result contract

Candidates are bounded to eight per stage. Selection reuses original
`crop_quality.scoreCrop` / `bestIndex` and `recognition_views.distinct_indices`.
It begins with a valid C4 anchor, prefers a distinct C3, fall and landing view,
then fills from remaining useful candidates up to four. Simple in-plane quarter
turns are also deduplicated. Missing stages never impose a count gate; one valid
C4 image is sufficient. Featureless or malformed optional crops are omitted.

`submit(journey, provider)` freezes available candidates and launches quality
selection, deduplication and provider I/O off the caller's control tick. Selection
does not hold the caller's lock. The original `_classifyImages` builds one
multipart/JPEG Brickognize request using the same UUID. There are no parallel
single-image requests, automatic retries or photographic transport retries.

`RecognitionRequest` and `RecognitionResult` carry `piece_uuid`, `episode_id`
and `generation`. Requests retain exact selected observation metadata. A result
is data only; it never routes, discards or commits a pocket. Closing a journey
rejects late results and removes it from the manager, while a small UUID
tombstone prevents a second request. The caller may retain the result/evidence
before releasing its own journey reference.

## Validation and limits

88 focused tests passed: journey association and request tests, original C3
collector tests and original multiview tests. Ruff passed. Independent review
approved after its adversarial association findings were fixed; the reviewer
independently passed 16 regression cases.

A broader focused check of unchanged `test_crop_quality.py` had one pre-existing
failure: `test_relative_margin_ships_fewer_frames_when_burst_is_mixed`. The
existing selector returns all geometrically eligible crops when they fit the
count limit; the test expects a blur margin that the implementation does not
apply. Its source and tests match the pre-slice hashes. They were not changed,
skipped, or relabeled as passing. The new selector's existing sharpness ranking
and recorded same-piece sharp-vs-blurred fixture test passed.

The complete journey replay uses synthetic images and explicit reconstructed
association evidence. Existing same-piece historical JPEG fixtures validate
quality ranking separately. No historical complete C3-to-C4 sequence with the
required generation/alias provenance was found; historical pixels alone are not
claimed as proof of cross-camera identity.

[Example journey and exact selected views](../../../analysis_artifacts/recognition-journey-slice2-20260922/example-journey.json)

[Protected-file verification](../../../analysis_artifacts/recognition-journey-slice2-20260922/verification.json)
