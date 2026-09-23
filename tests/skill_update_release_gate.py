"""Zero-provider-cost regression gate for Skill replacement and state upgrades."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

PROVIDER_CREDENTIALS = (
    "ANTHROPIC_API_KEY",
    "ARK_API_KEY",
    "VOLC_ASR_API_KEY",
    "VOLC_AK",
    "VOLC_SK",
    "VOLC_TTS_API_KEY",
    "SUNO_API_KEY",
)

TEST_MODULES = (
    "tests.test_runtime_backup",
    "tests.test_runtime_profile",
    "tests.test_fresh_install",
    "tests.test_skill_distribution",
)


def main() -> int:
    previous = {name: os.environ.get(name) for name in PROVIDER_CREDENTIALS}
    previous_runtime_home = os.environ.get("FACTORY_RUNTIME_HOME")
    with tempfile.TemporaryDirectory(prefix="ai-video-factory-update-gate-") as tmp:
        os.environ["FACTORY_RUNTIME_HOME"] = str(Path(tmp) / "runtime-home")
        for name in PROVIDER_CREDENTIALS:
            os.environ.pop(name, None)
        try:
            suite = unittest.defaultTestLoader.loadTestsFromNames(TEST_MODULES)
            result = unittest.TextTestRunner(verbosity=1).run(suite)
        finally:
            if previous_runtime_home is None:
                os.environ.pop("FACTORY_RUNTIME_HOME", None)
            else:
                os.environ["FACTORY_RUNTIME_HOME"] = previous_runtime_home
            for name, value in previous.items():
                if value is not None:
                    os.environ[name] = value
    if not result.wasSuccessful():
        return 1
    print("SKILL_UPDATE_RELEASE_GATE_OK paid_provider_calls=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
