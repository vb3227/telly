"""Validated reuse of saved behaviors; parameter changes never create a skill."""

import copy
import re
from program_validation import validate_program

PARAMETERS = {
    "rgb.blink": {"r", "g", "b", "brightness", "count", "period_ms"},
    "rgb.set": {"r", "g", "b", "brightness"},
    "display.text": {"text"},
}
NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "один": 1,
    "два": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
}
COLORS = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "purple": (160, 0, 255),
    "white": (255, 255, 255),
    "красным": (255, 0, 0),
    "зелёным": (0, 255, 0),
    "зеленым": (0, 255, 0),
    "синим": (0, 0, 255),
    "жёлтым": (255, 255, 0),
    "желтым": (255, 255, 0),
    "фиолетовым": (160, 0, 255),
    "белым": (255, 255, 255),
}


def canonical(step):
    if step["op"] == "rgb.blink":
        return {"r": 40, "g": 40, "b": 40, "brightness": 5, "count": 3, "period_ms": 250, **step}
    return dict(step)


def expand(parts, items):
    if not isinstance(parts, list) or not 1 <= len(parts) <= 8:
        raise ValueError("Composition needs 1–8 saved skills")
    steps = []
    for part in parts:
        if not isinstance(part, dict) or set(part) - {"skill", "parameters", "repeat"}:
            raise ValueError("Invalid composition fields")
        item = items.get(part.get("skill"))
        if not item or item.get("runtime") == "python":
            raise ValueError("Composition requires a saved JSON skill")
        repeat = part.get("repeat", 1)
        if type(repeat) is not int or not 1 <= repeat <= 8:
            raise ValueError("Repeat must be 1–8")
        params = part.get("parameters", {})
        if not isinstance(params, dict):
            raise ValueError("Invalid parameters")
        block = copy.deepcopy(item["program"]["steps"])
        for name, value in params.items():
            candidates = [step for step in block if name in PARAMETERS.get(step["op"], set())]
            if len(candidates) != 1:
                raise ValueError("Ambiguous or unsupported parameter")
            candidates[0][name] = value
        steps.extend(copy.deepcopy(block) * repeat)
    return validate_program({"steps": steps})


def local_composition(text, catalog, normalize):
    value = normalize(text)
    value = re.sub(r"\b(?:twice|дважды)\b", "2 times", value)
    value = re.sub(r"\b(?:once|однократно)\b", "1 times", value)
    clauses = re.split(r"\s+(?:and then|и затем|then|затем|потом|and|и)\s+", value)
    if len(clauses) > 8:
        return None
    items, _, _ = catalog.snapshot()
    parts = []
    for clause in clauses:
        exact = catalog.find(clause)
        if exact and items.get(exact, {}).get("runtime") != "python":
            parts.append({"skill": exact})
            continue
        match = re.fullmatch(
            r"(?:blink|flash|мигни|помигай)(?: the)? ("
            + "|".join(COLORS)
            + r")(?: lights| leds| светом| светодиодами)?(?: (\d+|"
            + "|".join(NUMBERS)
            + r") (?:times|раз|раза))?",
            clause,
        )
        if match and "blink" in items:
            count = NUMBERS.get(match[2], int(match[2]) if match[2] and match[2].isdigit() else 1)
            r, g, b = COLORS[match[1]]
            parts.append({"skill": "blink", "parameters": {"r": r, "g": g, "b": b, "count": count}})
            continue
        repeat = re.fullmatch(
            r"(?:repeat|повтори) (.+) (\d+|" + "|".join(NUMBERS) + r") (?:times|раз|раза)", clause
        )
        if repeat:
            exact = catalog.find(repeat[1])
            count = NUMBERS.get(repeat[2], int(repeat[2]) if repeat[2].isdigit() else 1)
            if exact and items.get(exact, {}).get("runtime") != "python":
                parts.append({"skill": exact, "repeat": count})
                continue
        return None
    if not parts:
        return None
    try:
        return {
            "kind": "program",
            "program": expand(parts, items),
            "sources": [p["skill"] for p in parts],
            "route": "composed",
        }
    except ValueError:
        return None


def reuse_program(program, items):
    """Find an exact parameterized cover, preserving every step and its order."""
    validate_program(program)
    target = program["steps"]
    templates = [item for item in items.values() if item.get("runtime") != "python"]
    cache = {}

    def visit(offset):
        if offset == len(target):
            return []
        if offset in cache:
            return cache[offset]
        cache[offset] = None
        for item in templates:
            block = item["program"]["steps"]
            if offset + len(block) > len(target):
                continue
            params = {}
            valid = True
            for source, wanted in zip(block, target[offset : offset + len(block)]):
                source, wanted = canonical(source), canonical(wanted)
                if source.get("op") != wanted.get("op"):
                    valid = False
                    break
                for name in source.keys() | wanted.keys():
                    if source.get(name) != wanted.get(name):
                        if name not in PARAMETERS.get(source["op"], set()) or name in params:
                            valid = False
                            break
                        params[name] = wanted.get(name)
                if not valid:
                    break
            if not valid:
                continue
            part = {"skill": item["name"], "parameters": params}
            try:
                resolved = expand([part], items)["steps"]
            except ValueError:
                continue
            if list(map(canonical, resolved)) != list(
                map(canonical, target[offset : offset + len(block)])
            ):
                continue
            tail = visit(offset + len(block))
            if tail is not None and len(tail) < 8:
                cache[offset] = [part] + tail
                return cache[offset]
        return None

    parts = visit(0)
    return parts
