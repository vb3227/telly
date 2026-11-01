import re
import unittest
from pathlib import Path


class DisplayFontTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "src/telly_font_data.h").read_text(encoding="utf-8")
        self.entries = [
            (int(cp, 16), int(advance), bytes.fromhex(data.replace("0x", "").replace(",", " ")))
            for cp, advance, data in re.findall(r"\{0x([0-9A-F]+), (\d+), \{([^}]+)\}\}", text)
        ]

    def test_sorted_unique_glyphs_cover_russian_and_ascii(self):
        codes = [cp for cp, _, _ in self.entries]
        self.assertEqual(codes, sorted(set(codes)))
        for char in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя":
            self.assertIn(ord(char), codes)

    def test_bitmaps_fit_lvgl_descriptor_and_are_visible(self):
        for cp, advance, bitmap in self.entries:
            with self.subTest(cp=cp):
                self.assertEqual(len(bitmap), 20 * 20 // 8)
                self.assertGreater(advance, 0)
                self.assertLessEqual(advance, 20)
                if cp != ord(" "):
                    self.assertTrue(any(bitmap))

    def test_cyrillic_glyphs_are_distinct_not_placeholder(self):
        glyphs = {cp: data for cp, _, data in self.entries}
        for char in "ЖЁЙжёй":
            self.assertNotEqual(glyphs[ord(char)], glyphs[ord("?")])
        self.assertNotEqual(glyphs[ord("Е")], glyphs[ord("Ё")])
        self.assertNotEqual(glyphs[ord("И")], glyphs[ord("Й")])
