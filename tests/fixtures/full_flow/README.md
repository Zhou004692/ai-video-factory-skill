# Full-flow replay fixtures

Each scenario directory contains one `manifest.json` and optional media under
`media/`. The manifest is local test data, not a recording that may contain
credentials or signed URLs.

Rules:

- `fixture_schema_version` is currently `1`.
- `request_hash` is produced by `factory.provider_replay.fingerprint`.
- A response sequence may model queued, running and terminal provider states.
- Media paths are relative to the scenario root and may not escape it.
- Missing operations fail the run. Replay never falls back to a live provider.
- Fixtures must not contain API keys, tokens, account IDs or production paths.

