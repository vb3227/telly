def measure_light():
    readings = []
    for i in range(3):
        sensors = telly.sensors()
        value = None
        for key in ['light', 'illuminance', 'light_lux']:
            candidate = sensors.get(key)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                value = candidate
                break
        if value is None or not (0 <= value < float('inf')):
            telly.show('Ошибка: нет данных освещённости')
            return {'error': 'Нет корректного значения освещённости'}
        readings.append(value)
        if i < 2:
            telly.sleep(0.5)
    summary = {'min': min(readings), 'max': max(readings), 'mean': sum(readings) / len(readings)}
    telly.save(summary)
    telly.show(f"Свет\nМин: {summary['min']:.1f}\nМакс: {summary['max']:.1f}\nСреднее: {summary['mean']:.1f}")
    return summary

result = measure_light()
