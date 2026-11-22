"""Vehicle identity and weighbridge evidence used by voyage decisions."""
from datetime import datetime


def _instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def matched(vehicle):
    return vehicle.get('scan_vin') == vehicle['vin']


def measured(vehicle):
    return (vehicle.get('measured_kg') is not None
            and vehicle.get('weigh_calibrated')
            and _instant(vehicle['calibration_start']) <= _instant(vehicle['weighed_at'])
            < _instant(vehicle['calibration_end']))

