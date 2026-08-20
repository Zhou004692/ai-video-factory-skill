# Changelog

All notable changes to AI Video Factory are documented here.

## [3.0.2.0] - 2026-08-20

### Added

- Added bounded parallel generation for independent Seedance masters while preserving scene, character and continuity dependencies.
- Added configurable reference-video attention budgets and task-first recovery evidence for paid provider calls.

### Changed

- Improved Seedance 2.5 prompts, duration planning, native speech timelines and continuity reference selection around longer cinematic masters.
- Improved management-console recovery guidance for provider account failures and policy-rejected jobs.
- Improved subtitle segmentation so captions follow semantic speech phrases instead of splitting words at fixed character counts.

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
