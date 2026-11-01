import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from repository_files import release_files
from check_repository import scan_text


class PublicationTests(unittest.TestCase):
    def test_manifest_excludes_secrets_runtime_and_generated_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            for name in [
                "README.md",
                "include/board_config.example.h",
                "include/board_config.h",
                ".telly/frame.jpg",
                ".codex/config.toml",
                "work/env.py",
                "mcp/__pycache__/x.pyc",
                "tests/test_example.py",
                "src/main.cpp",
                "docs/assets/telly-banner.svg",
                ".pio/firmware.bin",
            ]:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("placeholder")
            selected = {p.relative_to(root).as_posix() for p in release_files(root)}
            self.assertEqual(
                selected,
                {
                    "README.md",
                    "include/board_config.example.h",
                    "tests/test_example.py",
                    "src/main.cpp",
                    "docs/assets/telly-banner.svg",
                },
            )

    def test_actual_credential_and_private_key_are_flagged_without_values(self):
        credential = "example-local-credential"
        findings = scan_text("token=" + credential, [credential])
        self.assertEqual(findings, ["actual local credential"])
        self.assertNotIn(credential, " ".join(findings))
        self.assertIn(
            "credential/private-key pattern", scan_text("-----BEGIN " + "PRIVATE KEY-----")
        )

    def test_documentation_address_is_allowed(self):
        self.assertEqual(scan_text("http://192.0.2.1:8765"), [])
        self.assertIn("private-network address", scan_text("http://" + "10." + "1.2.3"))
