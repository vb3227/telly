"""Offline publication checks. Reports paths/reasons, never credential values."""

import json
from pathlib import Path
import re
import sys
import subprocess
from urllib.parse import unquote
from repository_files import release_files

ROOT = Path(__file__).resolve().parents[1]
KEY_PATTERN = re.compile(
    r"(?:gsk_[A-Za-z0-9]{20,}|sk-(?:proj-)?[A-Za-z0-9_-]{24,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)
MACHINE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]Users[\\/][^\s<>]+|/" + "Users" + r"/[^/\s]+/)")
PRIVATE_IP = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
)


def local_secrets(root):
    path = Path(root) / "include" / "board_config.h"
    if not path.exists():
        return []
    source = path.read_text(encoding="utf-8")
    names = "WIFI_SSID|WIFI_PASSWORD|BOARD_TOKEN|GROQ_API_KEY|VOICE_GATEWAY_TOKEN"
    values = re.findall(r"^\s*#define\s+(?:" + names + r')\s+"([^"\r\n]*)"', source, re.M)
    return [value for value in values if len(value) >= 8]


def scan_text(text, secrets=()):
    reasons = []
    if KEY_PATTERN.search(text):
        reasons.append("credential/private-key pattern")
    if MACHINE_PATH.search(text):
        reasons.append("personal workstation path")
    if PRIVATE_IP.search(text):
        reasons.append("private-network address")
    if any(secret in text for secret in secrets):
        reasons.append("actual local credential")
    return reasons


def check(root=ROOT):
    root = Path(root).resolve()
    issues = []
    files = release_files(root)
    if (root / ".git").exists():
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            capture_output=True,
            check=True,
        )
        candidates = set(result.stdout.decode("utf-8").strip("\0").split("\0")) - {""}
        selected = {p.relative_to(root).as_posix() for p in files}
        for unexpected in sorted(candidates - selected):
            issues.append((unexpected, "Git candidate outside publication manifest"))
    secrets = local_secrets(root)
    sys.path.insert(0, str(root / "mcp"))
    from program_validation import validate_program

    for path in files:
        relative = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            issues.append((relative, "not UTF-8 source"))
            continue
        for reason in scan_text(text, secrets):
            issues.append((relative, reason))
        if path.parent == root / "skills" / "python" and path.suffix == ".json":
            try:
                from python_skills import read_python_skill

                read_python_skill(path.parent, path.stem)
            except (OSError, ValueError, TypeError):
                issues.append((relative, "invalid Python skill/source manifest"))
        if path.parent == root / "skills" and path.suffix == ".json":
            try:
                skill = json.loads(text)
                if path.stem != skill["name"] or not re.fullmatch(
                    r"[a-z0-9_-]{1,31}", skill["name"]
                ):
                    raise ValueError("invalid skill name")
                if (
                    not isinstance(skill["voice_phrase"], str)
                    or len(skill["voice_phrase"].encode()) > 63
                ):
                    raise ValueError("invalid voice phrase")
                validate_program(skill["program"])
            except (ValueError, KeyError, TypeError):
                issues.append((relative, "invalid example skill"))
        if path.suffix == ".md":
            for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
                target = target.strip("<>").split("#", 1)[0]
                if not target or ":" in target:
                    continue
                destination = (path.parent / unquote(target)).resolve()
                if not destination.is_relative_to(root) or not destination.exists():
                    issues.append((relative, "broken/outside local Markdown link"))
    return files, issues


def main():
    files, issues = check()
    for path, reason in issues:
        print(f"FAIL {path}: {reason}")
    print(f"Checked {len(files)} publishable source files; {len(issues)} issue(s).")
    return bool(issues)


if __name__ == "__main__":
    raise SystemExit(main())
