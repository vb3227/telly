import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from skill_router import SkillCatalog, FastRouter
from voice_policy import classify_transcript


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.save("red3", "blink red three times", 3)
        self.catalog = SkillCatalog(self.folder)
        self.routers = []

    def tearDown(self):
        for router in self.routers:
            router.pool.shutdown(wait=True)
            router.client.close()
        self.temp.cleanup()

    def save(self, name, phrase, count):
        (self.folder / (name + ".json")).write_text(
            json.dumps(
                {
                    "name": name,
                    "voice_phrase": phrase,
                    "program": {
                        "steps": [{"op": "rgb.blink", "r": 255, "g": 0, "b": 0, "count": count}]
                    },
                }
            )
        )

    def router(self, request):
        router = FastRouter(self.catalog, "fake", request)
        self.routers.append(router)
        return router

    def test_spoken_number_and_x_notation_reuse_without_model(self):
        router = self.router(lambda _: self.fail("same skill must not call a model"))
        for text in ("Blink red 3x", "Blink red 3 times.", "Blink red three times"):
            self.assertEqual(router.resolve(text)["name"], "red3")
        self.assertEqual(router.resolve("Turn the camera on.")["name"], "camera_on")
        self.assertEqual(router.resolve("Turn camera off")["name"], "camera_off")

    def test_hot_lookup_never_reads_files_or_calls_model(self):
        router = self.router(lambda _: self.fail("exact lookup called model"))
        with patch.object(Path, "read_text", side_effect=AssertionError("disk on hot path")):
            for _ in range(100):
                self.assertEqual(router.resolve("Please, blink red three times!")["name"], "red3")

    def test_local_conversation_and_explicit_code_skip_model(self):
        router = self.router(lambda _: self.fail("unnecessary model call"))
        self.assertEqual(router.resolve("Test.")["route"], "local_answer")
        for text in ("This is the second test.", "Second test", "Это второй тест.", "Тест номер 2"):
            self.assertEqual(router.resolve(text)["route"], "local_answer")
        self.assertEqual(router.resolve("Привет!")["kind"], "answer")
        for text in ("Напиши Python программу", "Create a C++ function"):
            self.assertEqual(router.resolve(text)["route"], "code_request")

    def test_ordinary_question_answered_in_one_call(self):
        calls = []
        router = self.router(
            lambda payload: calls.append(payload) or {"kind": "answer", "reply": "Париж."}
        )
        result = router.resolve("Какая столица Франции?")
        self.assertEqual(result["route"], "fast_answer")
        self.assertEqual(result["reply"], "Париж.")
        self.assertEqual(len(calls), 1)

    def test_answer_cannot_swallow_actions_live_readings_or_code(self):
        router = self.router(lambda _: {"kind": "answer", "reply": "Done."})
        for text in (
            "This is the second test, blink blue",
            "Turn the camera on and explain lenses",
            "Create a pattern on the LEDs",
            "Какая температура сейчас?",
            "What do you see?",
            "read the sensors",
        ):
            with self.subTest(text=text):
                self.assertEqual(router.resolve(text)["kind"], "complex")

    def test_answer_schema_and_complex_delegation(self):
        for output in (
            {"kind": "answer", "reply": ""},
            {"kind": "answer", "reply": 12},
            {"kind": "answer", "reply": "hello", "program": {}},
            {"kind": "complex"},
        ):
            router = self.router(lambda _, output=output: output)
            self.assertEqual(router.resolve("Explain quantum mechanics?")["kind"], "complex")

    def test_conversation_provider_failure_does_not_cascade_to_codex(self):
        def fail(_):
            raise ConnectionError("offline")

        router = self.router(fail)
        for text in ("Почему небо синее?", "What is gravity?"):
            result = router.resolve(text)
            self.assertEqual(result["route"], "fast_unavailable")
            self.assertEqual(result["kind"], "answer")
        self.assertEqual(router.resolve("draw an unknown shape")["kind"], "complex")

    def test_saved_skill_precedes_local_conversation(self):
        self.save("test_skill", "test", 2)
        self.catalog.refresh()
        router = self.router(lambda _: self.fail("saved skill called model"))
        self.assertEqual(router.resolve("Test.")["name"], "test_skill")

    def test_paraphrase_gets_complete_catalog_and_is_cached(self):
        payloads = []
        router = self.router(lambda p: (payloads.append(p) or {"kind": "existing", "name": "red3"}))
        text = "Make the red LEDs flash three times"
        self.assertEqual(router.resolve(text)["name"], "red3")
        with patch.object(Path, "read_text", side_effect=AssertionError("disk")):
            self.assertEqual(router.resolve(text)["route"], "cached")
        self.assertEqual(len(payloads), 1)
        self.assertIn('"count":3', payloads[0]["messages"][0]["content"])

    def test_wrong_count_color_and_stop_are_rejected(self):
        for text, name in [
            ("make red flash four times", "red3"),
            ("make green flash three times", "red3"),
            ("stop the camera", "camera_on"),
        ]:
            router = self.router(lambda p, name=name: {"kind": "existing", "name": name})
            # Force a model choice that contradicts the requested stop action.
            self.assertEqual(router.resolve(text)["kind"], "complex")

    def test_invalid_model_output_cannot_execute(self):
        for output in [
            {"kind": "existing", "name": "invented"},
            {
                "kind": "new",
                "name": "evil",
                "voice_phrase": "evil",
                "program": {"steps": [{"op": "shell", "cmd": "x"}]},
            },
        ]:
            self.assertEqual(
                self.router(lambda _, o=output: o).resolve("make something new")["kind"], "complex"
            )

    def test_valid_new_plan_and_catalog_refresh(self):
        plan = {
            "kind": "new",
            "name": "red4",
            "voice_phrase": "blink red four times",
            "program": {"steps": [{"op": "rgb.blink", "r": 255, "count": 4}]},
        }
        router = self.router(lambda _: plan)
        self.assertEqual(router.resolve("make red flash four times")["kind"], "program")
        self.save("red4", "blink red four times", 4)
        self.catalog.refresh()
        self.assertEqual(router.resolve("blink red four times")["route"], "exact")

    def test_catalog_change_invalidates_semantic_cache(self):
        calls = []
        router = self.router(lambda p: (calls.append(p) or {"kind": "existing", "name": "red3"}))
        router.resolve("make red flash three times")
        self.save("red3", "blink red three times", 5)
        self.catalog.refresh()
        self.assertEqual(router.resolve("make red flash three times")["kind"], "complex")
        self.assertEqual(len(calls), 2)

    def test_slow_model_is_bounded_and_cannot_accumulate_requests(self):
        def slow(_):
            time.sleep(1.15)
            return {"kind": "existing", "name": "red3"}

        router = self.router(slow)
        start = time.monotonic()
        self.assertEqual(router.resolve("make red flash three times")["kind"], "complex")
        self.assertLess(time.monotonic() - start, 1.3)
        self.assertEqual(router.resolve("another request")["kind"], "complex")

    def test_manual_input_has_no_verb_whitelist(self):
        for text in (
            "Camera off.",
            "Camera on",
            "Purple five times",
            "Все датчики",
            "A purple square",
        ):
            self.assertEqual(classify_transcript(text, manual=True), "command")
        self.assertEqual(classify_transcript("Hi Telly, camera off", manual=True), "local")
        self.assertEqual(classify_transcript(" ", manual=True), "ignore")
        self.assertEqual(classify_transcript("background conversation"), "ignore")
        router = self.router(lambda _: self.fail("camera shortcuts must not call model"))
        self.assertEqual(router.resolve("Camera off.")["name"], "camera_off")
        self.assertEqual(router.resolve("Camera on.")["name"], "camera_on")

    def test_camera_word_order_is_not_ignored(self):
        for text in ("Turn the camera on.", "Turn camera on.", "Turn camera off."):
            self.assertEqual(classify_transcript(text), "command")

    def test_polite_paraphrases_and_local_wake(self):
        self.assertEqual(classify_transcript("Could you please show all sensors?"), "command")
        self.assertEqual(classify_transcript("Hi Telly, show all sensors"), "local")
        self.assertEqual(classify_transcript("I watched a camera video"), "ignore")


if __name__ == "__main__":
    unittest.main()
