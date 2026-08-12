# Changelog

All notable changes to AI Video Factory are documented here.

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
