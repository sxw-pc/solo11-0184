from datetime import datetime

def _instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))

def free(request, reservations):
    start, end = _instant(request['start']), _instant(request['end'])
    return all(
        other['berth_id'] != request['berth_id']
        or end <= _instant(other['start'])
        or _instant(other['end']) <= start
        for other in reservations
    )
