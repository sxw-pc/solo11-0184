from . import berth

def assign(request, reservations):
    return {'slot': request['slot_id'], 'result': '可用' if berth.free(request, reservations) else '冲突'}
