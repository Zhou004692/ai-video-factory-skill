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
from runtime_paths import (
    _promote_staging_directory,
    environment_file,
    prepare_runtime_state,
    read_environment_file,
    runtime_home,
)


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
    "scripts/distribute_skill.py",
    "scripts/doctor.py",
    "scripts/install.py",
    "scripts/management_server.py",
    "scripts/open_management.ps1",
    "scripts/requirements.txt",
    "scripts/runtime_lock.py",
    "scripts/runtime_filesystem.py",
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

# Explicit independent feature allowlist: never package private experiments,
# model weights, media or local dependency trees alongside the reusable entry.
RECREATION_FILES = {
    'scripts/recreation/bootstrap.py',
    'scripts/recreation/inspect_media.py',
    'references/recreation/initialization.md',
    'references/recreation/workflow.md',
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
    ".js", ".css", ".html",
}

MANAGEMENT_UI_REQUIRED_FILES = {
    'scripts/management_ui/' + name for name in (
        'index.html', 'styles.css', 'app.js', 'api-client.js', 'presentation.js',
        'ui-components.js', 'ui-tokens.css', 'workbench.js', 'workbench.css',
        'review-workspace.js', 'review-workspace.css', 'production-settings.js',
        'content-workspace.js', 'publication-workspace.js',
    )
}

# Local design experiments have private fixture characters and no production
# dependency. Keep their source intact while excluding the entire page bundles.
MANAGEMENT_UI_DESIGN_FILES = {
    'scripts/management_ui/' + stem + suffix
    for stem in ('design-options', 'studio-concepts')
    for suffix in ('.html', '.js', '.css')
}

AUDIT_RULES = {
    "credential-assignment": re.compile(
        r"(?im)(?:"
        r"^\s*(?:(?:const|let|var)\s+)?[\"']?(?:ARK_API_KEY|ANTHROPIC_API_KEY|OPENAI_API_KEY|VOLC_AK|"
        r"VOLC_SK|VOLC_ASR_API_KEY|VOLC_ASR_TOKEN|VOLC_TTS_API_KEY|KLING_SECRET_KEY|SUNO_API_KEY|"
        r"DOUYIN_CLIENT_SECRET|API_KEY|CLIENT_SECRET)[\"']?\s*(?:=|:)\s*"
        r"(?:[\"'](?!<|\.\.\.|optional|required)[^\"'\r\n]{8,}[\"']|"
        r"(?!<|\.\.\.|optional|required)[A-Za-z0-9][A-Za-z0-9._/-]{7,})\s*;?\s*(?:(?:#|//).*)?$"
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
    if relative.as_posix() in MANAGEMENT_UI_DESIGN_FILES:
        return False
    if any(part in EXCLUDED_PARTS for part in relative.parts):
        return False
    if relative.as_posix() in SCRIPT_ROOT_FILES | REFERENCE_FILES | RECREATION_FILES:
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


def _has_substantive_files(directory: Path) -> bool:
    return directory.is_dir() and any(
        path.is_file() and path.name != ".gitkeep"
        for path in directory.rglob("*")
    )


def _configured_in_skill_state(target: Path) -> bool:
    values = read_environment_file(environment_file(target, os.environ))
    configured = str(
        os.environ.get("FACTORY_DATA_DIR")
        or values.get("FACTORY_DATA_DIR")
        or ""
    ).strip()
    if not configured:
        return False
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        candidate = target / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(target.resolve())
    except ValueError:
        return False
    return _has_substantive_files(candidate)


def _populate_distribution(source_root: Path, target: Path) -> None:
    target.mkdir(parents=True)
    # Do not traverse local runtimes, verification venvs or nested worktrees.
    top_directories = set(DIRECTORY_RULES) | {"scripts", "references"}
    for directory, children, files in os.walk(source_root):
        parent = Path(directory)
        if parent == source_root:
            children[:] = [name for name in children if name in top_directories]
        else:
            children[:] = [name for name in children if name not in EXCLUDED_PARTS
                           and not name.startswith(".")]
        for name in files:
            path = parent / name
            if not _allowed(path, source_root):
                continue
            destination = target / path.relative_to(source_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)

    roster = target / "references" / "characters" / "roster.yaml"
    roster.parent.mkdir(parents=True, exist_ok=True)
    roster.write_text(_generic_roster(), encoding="utf-8")
    (target / "data").mkdir(exist_ok=True)
    (target / "data" / ".gitkeep").write_text("", encoding="utf-8")


def _recover_interrupted_replacement(output_root: Path, target: Path) -> None:
    if target.exists():
        return
    candidates = []
    for workspace in output_root.glob(".skill-replacement-*"):
        if workspace.is_symlink() or not workspace.is_dir():
            continue
        try:
            journal = json.loads((workspace / "replacement.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        previous = workspace / "previous"
        if journal.get("target") == str(target) and previous.is_dir() \
                and not previous.is_symlink():
            candidates.append((workspace, previous))
    if len(candidates) > 1:
        raise RuntimeError("Multiple interrupted Skill replacements require manual diagnosis")
    if candidates:
        workspace, previous = candidates[0]
        _promote_staging_directory(previous, target)
        shutil.rmtree(workspace, ignore_errors=True)


def build_distribution(source_root: Path, output_root: Path) -> Path:
    """Validate a staged package before replacing code; restore on failure."""
    source_root = source_root.resolve()
    output_root = output_root.resolve()
    target = output_root / SKILL_NAME
    if source_root == target or source_root.is_relative_to(target):
        raise ValueError("Skill source must be staged outside the replacement target")
    if target.is_symlink() or target.resolve() != target:
        raise ValueError("Skill replacement target must stay inside the output directory")
    for key in ("FACTORY_RUNTIME_HOME", "FACTORY_ENV_FILE", "FACTORY_DATA_DIR", "FACTORY_DB_PATH", "AI_VIDEO_RECREATION_HOME"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            candidate = Path(value).expanduser()
            if candidate.is_absolute() and candidate.resolve().is_relative_to(target):
                raise RuntimeError(f"{key} 指向可替换的 Skill 目录；请改为目录之外的路径后重试")
    if output_root.is_relative_to(source_root) and output_root != source_root:
        first_part = output_root.relative_to(source_root).parts[0]
        if first_part in {"scripts", "references", "templates", "agents"}:
            raise ValueError("Skill output must stay outside distributable source directories")
    output_root.mkdir(parents=True, exist_ok=True)
    if not target.exists() and any(output_root.glob(".skill-replacement-*/previous")):
        with exclusive_runtime_lock(runtime_home(target, os.environ), purpose="恢复 Skill 替换"):
            _recover_interrupted_replacement(output_root, target)
    workspace = Path(tempfile.mkdtemp(prefix=".skill-replacement-", dir=output_root)).resolve()
    staged, previous = workspace / "next", workspace / "previous"
    (workspace / "replacement.json").write_text(
        json.dumps({"target": str(target)}), encoding="utf-8")
    committed = False
    try:
        _populate_distribution(source_root, staged)
        required = SCRIPT_ROOT_FILES | MANAGEMENT_UI_REQUIRED_FILES | RECREATION_FILES | {
            "SKILL.md", "VERSION", "templates/env.example",
            "scripts/factory/db.py", "scripts/management_ui/index.html",
            "scripts/management_ui/styles.css", "scripts/management_ui/app.js",
        }
        missing = sorted(name for name in required if not (staged / name).is_file())
        if missing:
            raise ValueError("Skill source is incomplete: " + ", ".join(missing))
        report = audit_distribution(staged)
        if not report["ok"]:
            raise ValueError(json.dumps(report, ensure_ascii=False))
        if not target.exists():
            _promote_staging_directory(staged, target)
            committed = True
            return target
        with exclusive_runtime_lock(runtime_home(target, os.environ), purpose="替换 Skill"):
            data_dir = target / "data"
            has_legacy_state = (target / ".env").is_file() or _has_substantive_files(data_dir)
            if has_legacy_state or _configured_in_skill_state(target):
                prepare_runtime_state(target, os.environ)
            _promote_staging_directory(target, previous)
            try:
                _promote_staging_directory(staged, target)
            except BaseException:
                _promote_staging_directory(previous, target)
                raise
            committed = True
    finally:
        # Never erase the only old copy if even rollback was interrupted or failed.
        if committed or not previous.exists():
            shutil.rmtree(workspace, ignore_errors=True)
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
        if relative == "scripts/distribute_skill.py":
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
    version = (source_root.resolve() / "VERSION").read_text(
        encoding="utf-8"
    ).strip()
    if not re.fullmatch(r"[0-9A-Za-z._-]+", version):
        raise ValueError("VERSION contains unsupported archive filename characters")
    package = build_distribution(source_root, output_root)
    archive = output_root.resolve() / f"{SKILL_NAME}-{version}.zip"
    with tempfile.TemporaryDirectory(prefix=".skill-archive-", dir=output_root) as tmp:
        staged_archive = Path(tmp) / archive.name
        with zipfile.ZipFile(staged_archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(package.rglob("*")):
                if path.is_file():
                    bundle.write(path, Path(SKILL_NAME) / path.relative_to(package))
        os.replace(staged_archive, archive)
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
    archive = build_archive(args.source, args.output)
    package = args.output.resolve() / SKILL_NAME
    report = audit_distribution(package)
    print(json.dumps({
        "package": str(package),
        "archive": str(archive) if archive else None,
        **report,
    }, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
