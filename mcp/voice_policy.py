"""Speech routing: a local wake word never creates a cloud hardware action."""

import re
import unicodedata

WAKE = re.compile(r"\b(?:hi|high|hey|хай|хэй|привет)[\s,!.:-]*(?:telly|telli|телли|тели)\b", re.I)
REQUEST = re.compile(
    r"^(?:telly[\s,:-]+|телли[\s,:-]+)?(?:пожалуйста[\s,]+)?(?:включи|выключи|мигни|покажи|сделай|создай|напиши|проверь|прочитай|останови|запусти|измени|проанализируй|посмотри|найди|объясни|turn|open|close|switch|flash|show|blink|start|stop|create|write|read|analy[sz]e|check|make|look|find|explain)\b",
    re.I,
)


def normalize(text):
    return unicodedata.normalize("NFKC", text).strip().casefold()


def coding_request(text):
    value = normalize(text)
    return bool(
        re.search(
            r"\b(?:напиш\w*|написать|созда\w*|исправ\w*|передела\w*|отла\w*|оптимизир\w*|write|create|fix|debug|implement|refactor)\b",
            value,
        )
        and re.search(
            r"\b(?:python|код\w*|программ\w*|скрипт\w*|навык\w*|code|script|program|skill|function)\b|\bc\+\+",
            value,
        )
    )


def can_answer_fast(text):
    """A text answer must not swallow a hardware action or invent live readings."""
    value = normalize(text)
    if WAKE.search(value) or coding_request(value):
        return False
    if re.search(
        r"\b(?:включ\w*|выключ\w*|отключ\w*|мигни|покажи|нарисуй|запуст\w*|останов\w*|сохрани\w*|установ\w*|сделай|создай|напиши|прочитай|проверь|проанализируй|blink|flash|enable|disable|switch|open|close|run|execute|install|save|show|draw|start|stop|turn|read|create|make|write|check|analy[sz]e)\b",
        value,
    ):
        return False
    if re.search(
        r"\b(?:сейчас|текущ\w*|здесь|в комнате|на плате|current|right now|in this room|your sensors?)\b",
        value,
    ) and re.search(
        r"температур|влажност|освещ|датчик|камер|temperature|humidity|light|sensor|camera", value
    ):
        return False
    if re.fullmatch(r"(?:скажи )?(?:какая температура|what is the temperature)[.!?\s]*", value):
        return False
    if re.search(
        r"(?:что ты видишь|что на камере|what do you see|camera image|video clip|видеофрагмент)",
        value,
    ):
        return False
    return True


def conversation_candidate(text):
    value = normalize(text)
    return can_answer_fast(value) and bool(
        re.match(
            r"^(?:кто|что|почему|зачем|как|где|когда|сколько|расскажи|объясни|ответь|скажи|what|who|why|how|where|when|tell|explain|answer)\b",
            value,
        )
        or value.endswith("?")
    )


def local_conversation_reply(text):
    value = re.sub(r"[.!?,]+$", "", normalize(text)).strip()
    # Whole phrases only: "test, blink red" is still an action request.
    if re.fullmatch(
        r"(?:this is (?:the )?)?(?:(?:first|second|third|another|next|\d{1,3}) )?(?:voice |connection )?test(?: number \d{1,3})?",
        value,
    ):
        return "Test received. I'm here."
    if re.fullmatch(
        r"(?:это )?(?:(?:первый|второй|третий|очередной|\d{1,3}) )?(?:тест|проверка связи)(?: номер \d{1,3})?",
        value,
    ):
        return "На связи. Запрос получен."
    return {
        "test": "Test received. I'm here.",
        "testing": "Test received. I'm here.",
        "тест": "На связи. Запрос получен.",
        "проверка": "На связи. Запрос получен.",
        "привет": "Привет! Чем помочь?",
        "здравствуй": "Привет! Чем помочь?",
        "hello": "Hello! How can I help?",
        "hi": "Hello! How can I help?",
        "спасибо": "Пожалуйста!",
        "thank you": "You're welcome.",
    }.get(value)


def classify_transcript(text, *, manual=False):
    if not isinstance(text, str) or not text.strip():
        return "ignore"
    if WAKE.search(normalize(text)):
        return "local"
    # Physical button capture is explicit input, not ambient speech.
    if manual or known_skill(text):
        return "command"
    value = re.sub(r"^(?:could you|can you|would you|можешь|можете)\s+", "", normalize(text))
    value = re.sub(r"^please\s+", "", value)
    return "command" if REQUEST.search(value) else "ignore"


def known_skill(text):
    value = re.sub(r"[.!?,]+$", "", normalize(text)).strip()
    camera = re.fullmatch(r"turn (?:(on|off) (?:the )?camera|(?:the )?camera (on|off))", value)
    if camera:
        return "camera_on" if (camera[1] or camera[2]) == "on" else "camera_off"
    return {
        "camera on": "camera_on",
        "camera off": "camera_off",
        "включи камеру": "camera_on",
        "запусти камеру": "camera_on",
        "turn on the camera": "camera_on",
        "start camera": "camera_on",
        "выключи камеру": "camera_off",
        "останови камеру": "camera_off",
        "turn off the camera": "camera_off",
        "stop camera": "camera_off",
        "покажи датчики": "show_sensors",
        "покажи все датчики": "show_sensors",
        "show sensors": "show_sensors",
        "read all sensors": "show_sensors",
    }.get(value)


def remember_alias(name, text):
    import json
    from pathlib import Path

    if not re.fullmatch(r"[a-z0-9_-]{1,31}", name):
        return
    path = Path(__file__).resolve().parent.parent / "skills" / (name + ".json")
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
        aliases = item.get("aliases", [])
        value = re.sub(r"[.!?,]+$", "", normalize(text)).strip()
        item["aliases"] = list(dict.fromkeys([value] + aliases))[:8]
        path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError, TypeError):
        pass
