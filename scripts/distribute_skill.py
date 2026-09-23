"""Build a sanitized, user-neutral AI Video Factory skill directory."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import zipfile

from runtime_lock import exclusive_runtime_lock
from runtime_paths import prepare_runtime_state, runtime_home


SKILL_NAME = "ai-video-factory"

ROOT_FILES = (
    ".gitignore",
    "CHANGELOG.md",
    "SKILL.md",
    "VERSION",
)

DIRECTORY_RULES = {
    "agents": ("*.yaml",),
    "templates": ("env.example", "topics.csv.example"),
}

SCRIPT_ROOT_FILES = {
    "scripts/bootstrap.py",
    "scripts/cli.py",
    "scripts/doctor.py",
    "scripts/install.py",
    "scripts/management_server.py",
    "scripts/open_management.ps1",
    "scripts/requirements.txt",
    "scripts/runtime_lock.py",
    "scripts/runtime_paths.py",
}

REFERENCE_FILES = {
    "references/providers.md",
    "references/troubleshooting.md",
    "references/workflow.md",
    "references/characters/roster.yaml",
    "references/prompts/compliance.md",
    "references/prompts/storyboard.md",
    "references/prompts/topic_score.md",
    "references/prompts/voiceover_polish.md",
    "references/quality_canary_scripts.json",
}

EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    "data",
    "diagrams",
    "docs",
    "tests",
    "sales_xiaoneng",
    "experiments",
}

TEXT_EXTENSIONS = {
    ".md", ".yaml", ".yml", ".json", ".py", ".ps1", ".cmd", ".txt", ".example",
}

AUDIT_RULES = {
    "credential-assignment": re.compile(
        r"(?im)(?:"
        r"^\s*[\"']?(?:ARK_API_KEY|ANTHROPIC_API_KEY|OPENAI_API_KEY|VOLC_AK|"
        r"VOLC_SK|VOLC_ASR_API_KEY|VOLC_ASR_TOKEN|VOLC_TTS_API_KEY|KLING_SECRET_KEY|SUNO_API_KEY|"
        r"DOUYIN_CLIENT_SECRET|API_KEY|CLIENT_SECRET)[\"']?\s*(?:=|:)\s*"
        r"(?:[\"'](?!<|\.\.\.|optional|required)[^\"'\r\n]{8,}[\"']|"
        r"(?!<|\.\.\.|optional|required)[A-Za-z0-9][A-Za-z0-9._/-]{7,})\s*(?:#.*)?$"
        r"|[\"'](?:ARK_API_KEY|ANTHROPIC_API_KEY|OPENAI_API_KEY|VOLC_AK|"
        r"VOLC_SK|VOLC_ASR_API_KEY|VOLC_ASR_TOKEN|VOLC_TTS_API_KEY|KLING_SECRET_KEY|SUNO_API_KEY|"
        r"DOUYIN_CLIENT_SECRET|API_KEY|CLIENT_SECRET)[\"']\s*:\s*"
        r"[\"'](?!<|\.\.\.|optional|required)[^\"'\r\n]{8,}[\"']"
        r")"
    ),
    "bearer-token": re.compile(
        r"(?i)\bBearer[ \t]+(?!<|\.\.\.)[A-Za-z0-9._~-]{12,}"
    ),
    "jwt-token": re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}\b"
    ),
    "machine-bound-path": re.compile(r"(?i)(?:[A-Z]:\\Users\\|/Users/|/home/[^/\s]+/)"),
    "private-character": re.compile(
        r"小玲|sales_xiaoneng|asset://asset-(?!\.\.\.|<)[A-Za-z0-9._/-]+"
    ),
    "organization-default": re.compile(r"众能联合|ZNLH"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


def _allowed(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in EXCLUDED_PARTS for part in relative.parts):
        return False
    if relative.as_posix() == "scripts/distribute_skill.py":
        return False
    if relative.as_posix() in SCRIPT_ROOT_FILES | REFERENCE_FILES:
        return True
    if relative.parts[:2] == ("scripts", "factory") and path.suffix == ".py":
        return True
    if relative.parts[:2] == ("scripts", "management_ui"):
        return path.suffix in {".html", ".css", ".js"}
    if relative.as_posix() in ROOT_FILES:
        return True
    if not relative.parts:
        return False
    patterns = DIRECTORY_RULES.get(relative.parts[0])
    if not patterns:
        return False
    return any(path.match(pattern) for pattern in patterns)


def _generic_roster() -> str:
    return """# Optional reusable character roster.
# Add only fictional characters or properly authorized real-person assets.
# Real people must use a provider-approved asset URI; never distribute private images.

host_demo:
  name: 示例主持人
  gender: female
  identity_type: fictional
  look: \"30 岁中国女性行业顾问，简洁职业装，自然真实质感\"
  typical_scenarios: [行业口播, 产品讲解]
  catchphrases: []
  brand_role: \"Replace this demo character with your own authorized character\"
"""


def build_distribution(source_root: Path, output_root: Path) -> Path:
    source_root = source_root.resolve()
    output_root = output_root.resolve()
    target = output_root / SKILL_NAME
    if target.exists():
        data_dir = target / "data"
        has_legacy_state = (target / ".env").is_file()
        if data_dir.is_dir():
            has_legacy_state = has_legacy_state or any(
                path.is_file() and path.name != ".gitkeep"
                for path in data_dir.rglob("*")
            )
        if has_legacy_state:
            with exclusive_runtime_lock(
                runtime_home(target, os.environ),
                purpose="替换 Skill",
            ):
                prepare_runtime_state(target, os.environ)
                shutil.rmtree(target)
        else:
            shutil.rmtree(target)
    target.mkdir(parents=True)

    for path in source_root.rglob("*"):
        if not path.is_file() or not _allowed(path, source_root):
            continue
        relative = path.relative_to(source_root)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)

    roster = target / "references" / "characters" / "roster.yaml"
    roster.parent.mkdir(parents=True, exist_ok=True)
    roster.write_text(_generic_roster(), encoding="utf-8")
    (target / "data").mkdir(exist_ok=True)
    (target / "data" / ".gitkeep").write_text("", encoding="utf-8")
    return target


def audit_distribution(package_root: Path) -> dict:
    findings: list[dict[str, str]] = []
    for path in package_root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(package_root).as_posix()
        suffix = path.suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".mp4", ".mov", ".webm", ".mkv", ".avi"}:
            findings.append({"rule": "private-media", "path": relative})
        if suffix in {".db", ".sqlite", ".sqlite3"}:
            findings.append({"rule": "local-database", "path": relative})
        if suffix not in TEXT_EXTENSIONS:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for rule, pattern in AUDIT_RULES.items():
            if pattern.search(text):
                findings.append({"rule": rule, "path": relative})
    forbidden = (".env", ".git", ".venv", "factory.db", "docs", "diagrams", "tests")
    for name in forbidden:
        if any(path.name == name for path in package_root.rglob("*")):
            findings.append({"rule": "forbidden-path", "path": name})
    return {"ok": not findings, "findings": findings}


def build_archive(source_root: Path, output_root: Path) -> Path:
    package = build_distribution(source_root, output_root)
    report = audit_distribution(package)
    if not report["ok"]:
        raise ValueError(json.dumps(report, ensure_ascii=False))
    version = (source_root.resolve() / "VERSION").read_text(
        encoding="utf-8"
    ).strip()
    if not re.fullmatch(r"[0-9A-Za-z._-]+", version):
        raise ValueError("VERSION contains unsupported archive filename characters")
    archive = output_root.resolve() / f"{SKILL_NAME}-{version}.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                bundle.write(path, Path(SKILL_NAME) / path.relative_to(package))
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--audit",
        action="store_true",
        help="build a temporary sanitized package and audit it without writing an archive",
    )
    args = parser.parse_args()
    if args.audit:
        with tempfile.TemporaryDirectory() as tmp:
            package = build_distribution(args.source, Path(tmp))
            report = audit_distribution(package)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ok"] else 1
    if args.output is None:
        parser.error("--output is required unless --audit is used")
    package = build_distribution(args.source, args.output)
    report = audit_distribution(package)
    archive = None
    if report["ok"]:
        archive = build_archive(args.source, args.output)
    print(json.dumps({
        "package": str(package),
        "archive": str(archive) if archive else None,
        **report,
    }, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
