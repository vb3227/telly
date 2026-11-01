"""Explicit publication boundary; never package a whole working directory."""

from pathlib import Path

ROOT_FILES = {
    "README.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "platformio.ini",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-local-stt.txt",
    "pyproject.toml",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
}
SOURCE_DIRS = {"src", "include", "mcp", "skills", "config", "tests", "scripts", "docs", ".github"}
SUFFIXES = {".py", ".cpp", ".h", ".md", ".json", ".toml", ".yml", ".yaml", ".txt", ".svg"}


def release_files(root):
    root = Path(root).resolve()
    paths = [root / name for name in ROOT_FILES if (root / name).is_file()]
    for name in SOURCE_DIRS:
        folder = root / name
        if folder.exists():
            paths.extend(p for p in folder.rglob("*") if p.is_file())
    editor = root / ".vscode" / "extensions.json"
    if editor.is_file():
        paths.append(editor)
    selected = []
    for path in paths:
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.name == "board_config.h":
            continue
        if path.suffix not in SUFFIXES and path.name not in ROOT_FILES:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Refusing external/symlink source: " + str(relative))
        selected.append(path)
    return sorted(set(selected), key=lambda p: p.relative_to(root).as_posix())
