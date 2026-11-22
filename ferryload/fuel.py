def consumed_liters(voyage):
    """Return a basic single-meter voyage difference when present."""
    meter = voyage.get('fuel_meter')
    if meter is None:
        return None
    amount = meter['end_liters'] - meter['start_liters']
    if amount < 0:
        raise ValueError('fuel meter moved backwards')
    return amount
