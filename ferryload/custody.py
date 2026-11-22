from . import seal

def assess(record):
    return {'cargo_id': record['cargo_id'],
            'result': '封签一致' if seal.unchanged(record) else '封签异常'}
