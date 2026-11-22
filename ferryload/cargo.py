def maximum_temperature_c(record):
    samples = record.get('samples', [])
    if not samples:
        return None
    return max(sample['temperature_c'] for sample in samples)
