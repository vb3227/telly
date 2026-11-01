"""Short reusable English triggers and Russian command aliases."""


def command_aliases(name, phrase, program=None, supplied=()):
    aliases = [name.replace("_", " "), phrase, *supplied]
    if program:
        ops = [step["op"] for step in program["steps"]]
        translations = {
            "camera.preview": "включи камеру",
            "camera.stop": "выключи камеру",
            "camera.photo": "сделай фото",
            "camera.record": "запиши видео",
            "camera.finish": "останови видео",
            "sensor.read.display": "покажи датчики",
            "rgb.off": "выключи свет",
        }
        if len(ops) == 1 and ops[0] in translations:
            aliases.append(translations[ops[0]])
    return list(
        dict.fromkeys(
            value.strip()
            for value in aliases
            if isinstance(value, str) and 0 < len(value.strip()) <= 63 and len(value.split()) <= 4
        )
    )[:8]
