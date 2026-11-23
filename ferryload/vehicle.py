"""Vehicle identity and weighbridge evidence used by voyage decisions."""
from datetime import datetime


def _instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def matched(vehicle):
    return vehicle.get('scan_vin') == vehicle['vin']


def measured(vehicle, book=None):
    ticket_no = vehicle.get('weigh_ticket')
    if ticket_no:
        # 引用磅票的车辆由共享偏差证书证据裁定，不再看 weigh_calibrated 布尔标志
        return book is not None and book.vehicle_valid(vehicle)
    return (vehicle.get('measured_kg') is not None
            and vehicle.get('weigh_calibrated')
            and _instant(vehicle['calibration_start']) <= _instant(vehicle['weighed_at'])
            < _instant(vehicle['calibration_end']))


def weight_upper_kg(vehicle, book=None):
    """Conservative upper bound of the vehicle's true weight.

    Ticketed vehicles weigh reading_kg - b with b >= bias_lo, so the upper
    bound is reading_kg - bias_lo of their shared group.  Returns None when a
    referenced ticket cannot supply a valid bound.
    """
    ticket_no = vehicle.get('weigh_ticket')
    if ticket_no:
        if book is None:
            return None
        ticket = book.ticket(ticket_no)
        if ticket is None or not book.ticket_valid(ticket):
            return None
        bias_lo, _ = book.bias_interval(book.group_key(ticket))
        return ticket['reading_kg'] - bias_lo
    return vehicle.get('measured_kg', 0)
