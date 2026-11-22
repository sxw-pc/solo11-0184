from . import duty

def eligible(entries, limit_minutes=480):
    return {'worked_minutes': duty.worked_minutes(entries),
            'result': '可排班' if duty.worked_minutes(entries) <= limit_minutes else '超时'}
