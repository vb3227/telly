readings = [telly.sensors() for n in range(3)]
values = [row["light"] for row in readings if row.get("light") is not None]
if values:
    average = round(statistics.mean(values), 1)
    telly.show("Средняя освещённость: " + str(average))
    telly.save({"average_light": average, "samples": len(values)})
    result = "Средняя освещённость: " + str(average)
else:
    result = "Датчик освещённости недоступен."
