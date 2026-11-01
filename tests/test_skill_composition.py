import unittest
import tempfile
from skill_router import SkillCatalog, FastRouter
from skill_composition import expand, reuse_program


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.catalog = SkillCatalog(self.folder.name)
        self.router = FastRouter(
            self.catalog, "", lambda _: self.fail("Known composition called a model")
        )

    def tearDown(self):
        self.router.pool.shutdown(wait=True)
        self.router.client.close()
        self.folder.cleanup()

    def test_parameter_changes_reuse_one_skill_without_model_or_catalog_growth(self):
        before = self.catalog.version
        for phrase, count in [
            ("мигни красным два раза", 2),
            ("blink red twice", 2),
            ("Jarvis blink blue 4 times", 4),
        ]:
            result = self.router.resolve(phrase)
            self.assertEqual(result["kind"], "program")
            self.assertEqual(result["program"]["steps"][0]["count"], count)
        self.assertEqual(self.catalog.version, before)

    def test_sequence_and_repeat_preserve_actions_and_order(self):
        result = self.router.resolve("show sensors and blink red twice")
        self.assertEqual(
            [v["op"] for v in result["program"]["steps"]], ["sensor.read.display", "rgb.blink"]
        )
        result = self.router.resolve("repeat show sensors 3 times")
        self.assertEqual(len(result["program"]["steps"]), 3)

    def test_invalid_parameter_injection_and_oversized_composition_are_rejected(self):
        items = self.catalog.snapshot()[0]
        for part in (
            {"skill": "blink", "parameters": {"count": 61}},
            {"skill": "blink", "parameters": {"op": "reset"}},
            {"skill": "blink", "repeat": True},
            {"skill": "unknown"},
        ):
            with self.assertRaises(ValueError):
                expand([part], items)
        self.assertEqual(
            self.router.resolve("blink red twice and do something unknown")["kind"], "complex"
        )

    def test_new_program_equivalent_to_existing_parameters_is_reusable(self):
        program = {
            "steps": [{"op": "rgb.blink", "r": 255, "count": 7}, {"op": "sensor.read.display"}]
        }
        parts = reuse_program(program, self.catalog.snapshot()[0])
        self.assertIsNotNone(parts)
        self.assertIsNone(
            reuse_program(
                {"steps": [{"op": "display.square", "size": 8}]}, self.catalog.snapshot()[0]
            )
        )

    def test_short_russian_commands_and_jarvis_prefix_are_indexed(self):
        self.assertEqual(self.catalog.find("Jarvis включи камеру"), "camera_on")
        self.assertEqual(self.catalog.find("сделай фото"), "take_photo")
