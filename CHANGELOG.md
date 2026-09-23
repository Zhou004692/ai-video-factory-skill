# Changelog

All notable changes to AI Video Factory are documented here.

## [3.2.0.2] - 2026-09-23

### Changed

- Separated mutable runtime state from the replaceable Skill directory, with automatic one-time migration of legacy `.env`, SQLite data and media assets.
- Changed upgrade snapshots to use SQLite's online backup API so committed WAL transactions are included consistently.
- Added a shared runtime lock so Skill upgrades fail closed while the management service is using mutable state.
- Isolated the Skill update release gate from real runtime paths and blocked all non-local network connections.

### Fixed

- Restored failed upgrades without leaving stale SQLite WAL/SHM sidecars that could override the recovered database.
- Preserved legacy state when an interrupted first run had already created an empty external data directory.
- Preserved genuine legacy state before an in-place distribution replacement deletes the old Skill tree.
- Rebased database paths that pointed into the legacy Skill data tree while leaving external paths unchanged.
- Migrated legacy relative custom data directories and hardcoded media subtrees into the stable runtime directory.
- Failed closed when legacy state conflicts with a populated, incomplete external runtime directory.
- Routed anchor recovery, management uploads and quality-canary evidence to the external runtime directory.

## [3.2.0.1] - 2026-09-20

### Changed

- Standardized delivery on the Skill directory or ZIP consumed by the Agent and removed the Windows installer and desktop-launcher distribution path.
- Kept local runtime initialization and management-console startup inside the Skill without creating desktop files or system installation entries.
- Added the quality-canary contract to the sanitized distribution manifest so a fresh Skill package can load the management overview.

## [3.2.0.0] - 2026-08-31

### Added

- Added R4 Flow Guard for continuous pairwise traversal of Production Graph v2 branches using Seedance 2.5 mocks, isolated SQLite databases and temporary local media only.
- Added machine-readable flow reports covering human reviews, budget pauses, provider reconciliation, recoverable contract failures and service restarts.
- Added a strict zero-cost repair allowlist for rebuilding reproducible projections, recovering expired leases and retrying repaired unpaid contract nodes.

### Changed

- Extended the zero-cost release gate with R4 branch exploration and management-console action reachability checks.
- Changed failed-video diagnosis actions to open the task doctor directly instead of only expanding technical error details.

### Fixed

- Fixed backend pause actions for provider reconciliation, quality review and automatic retry having no executable management-console route.
- Fixed isolated graph tests composing against the production database instead of the scenario database.

## [3.1.2.0] - 2026-08-28

### Added

- Added safe task diagnosis and repair in the management console, including a task-level entry that helps users recover blocked production jobs without editing runtime data manually.
- Added adaptive spatial continuity contracts with `free`, `continuity` and `locked` modes for cinematic character movement across Seedance masters.

### Changed

- Changed recurring-scene blocking from a fixed left/center/right layout to explicit incoming state, motivated movement and outgoing state handoff; exact position locking is now reserved for shots that require it.
- Updated Seedance prompts, continuity QA, management-console anchor visualization and product documentation to use the same spatial-continuity semantics.

### Fixed

- Fixed reasonable character movement being rejected as blocking drift in free-dispatch shots while preserving identity, scene and axis checks.
- Fixed the task doctor entry being hidden from the project-studio workflow.

## [3.1.1.0] - 2026-08-27

### Added

- Added a product-focused GitHub README covering core capabilities, the end-to-end user workflow, configuration tiers, installation, security boundaries and current limitations.

### Fixed

- Brief generation now recovers a missing story arc when it can be derived from the angle's existing hook, evidence and promise, while preserving strict validation for malformed model output.

## [3.1.0.0] - 2026-08-27

### Added

- Added segment-level performance reconstruction for local videos, including speaker evidence, speech-screen relationships, emotion, tone, pace, volume, expression, gaze, gesture and body action.
- Added an in-console performance review flow with synchronized source-video clips and editable corrections before script generation.

### Changed

- Changed local-video ingestion to run a fresh structural analysis for each content project instead of reusing stale source-analysis results.
- Passed reviewed source-performance evidence through script generation, production contracts and Seedance cinematic director prompts so generated performances preserve the source video's timing and character intent.
- Distinguished on-screen dialogue, off-screen speech and narration throughout casting, character anchors, master planning and video prompts.

### Fixed

- Fixed background dialogue being forced onto visible characters or generating unnecessary character anchors and lip-sync requirements.
- Fixed legacy approved projects failing compilation when their Creative IR character table was incomplete, while preserving reviewed runtime identities.
- Fixed floating-point timeline boundary errors and stale runtime-master mismatches that could block otherwise valid production jobs.

## [3.0.2.0] - 2026-08-20

### Added

- Added the automatic topic discovery MVP for authorized RSS, Atom and JSON Feed hotspot/competitor sources, including per-source schedules, cross-feed deduplication, traceable candidate review and conversion into content projects.
- Added bounded parallel generation for independent Seedance masters while preserving scene, character and continuity dependencies.
- Added configurable reference-video attention budgets and task-first recovery evidence for paid provider calls.
- Added cross-platform runtime profiles, external data-directory support, FFmpeg discovery, CJK font discovery and recoverable upgrade snapshots.

### Changed

- Changed character and recurring-scene anchor defaults to Seedance 2.5 dynamic anchors while keeping Seedream 5.0 as an explicit per-task option.
- Improved Seedance 2.5 prompts, duration planning, native speech timelines and continuity reference selection around longer cinematic masters.
- Improved management-console recovery guidance for provider account failures and policy-rejected jobs.
- Improved subtitle segmentation so captions follow semantic speech phrases instead of splitting words at fixed character counts.
- Versioned distributable archives and added desktop runtime/storage/media diagnostics without exposing credential values.

### Fixed

- Disabled provider image watermarks and in-video hook overlays by default while retaining marketing-thumbnail hooks.
- Fixed duplicate or misattributed paid submissions, unsafe provider-result downloads, unresolved-task recovery gaps and concurrent budget oversubscription.
- Fixed stalled review/reconciliation states, incomplete failure classification and dependent masters continuing after an unresolved provider task.

## [3.0.1.0] - 2026-08-20

### Added

- Added a local end-to-end management console for source intake, briefs, script review, video production, publication, configuration, todo handling and character assets.
- Added fixed, virtual and custom character assets, continuity anchors, configurable Seedream/Seedance anchor generation, native or external voice routing, and single-master lip-sync repair.
- Added durable provider task accounting, account circuit breaking, manual quality review, crash-safe reconciliation, bounded independent-master concurrency and a release fault matrix.

### Changed

- Improved Seedance 2.5 cinematic master generation with adaptive durations, continuous integer speech timelines, scene and character continuity constraints, and safer retry behavior.
- Improved subtitle timing, ASR segmentation, local director composition, BGM handling, source reconstruction and editable brief/script workflows.
- Hardened provider downloads, API mutation guards, cost ownership, recovery state transitions and local management-server safety.

### Fixed

- Fixed stuck queued or review jobs, duplicate paid submissions, cost drift, invalid state recovery, incomplete media playback, inconsistent character/scene anchors, subtitle truncation and lip-sync repair handoff.

## [3.0.0.0] - 2026-08-12

### Added

- Added a zero-command conversational workflow that initializes and operates the Skill on behalf of ordinary users.
- Added two script inputs: user-provided material and traceable source ingestion, with Brief generation, three script variants, approval and export.
- Added read-only Volcengine Assets API discovery for authorized real-person characters, with local binding and an `assetId` fallback when no unique match exists.
- Added production cost previews, provider-call accounting, retry budgets, B-roll reuse, performance feedback and controlled creative experiments.

### Changed

- Rebuilt standard production around Seedance 2.5 native picture, speech, ambience and lip sync.
- Changed the product core from generating every final shot to generating one to three native masters and performing local director editing with FFmpeg.
- Changed subtitles to use post-generation ASR timing and to omit terminal full stops while retaining question and exclamation marks.
- Changed distribution to a sanitized, user-neutral package that excludes credentials, local state, private characters and generated media.

### Removed

- Removed the standalone TTS, voice-cloning, OmniHuman and external lip-sync production path.
- Removed legacy smoke media, local validation artifacts and organization-specific defaults from the distributable Skill.
