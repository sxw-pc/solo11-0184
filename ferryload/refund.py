"""Refund responsibility after a cancelled sailing."""


def due(active, vehicle_rows, stows):
    return not active and any(not stows.get(vehicle['vin'], {}).get('boarded')
                              for vehicle in vehicle_rows)

