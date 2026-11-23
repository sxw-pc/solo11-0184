"""货箱交接账：消费封签谱系，输出逐箱交接结果。

结果区分：合法换封后完整交接、原封交接、未交接断链、无法归属待核；
旧版单段记录仍判封签一致/异常。
"""
from . import seal


def assess(record):
    if 'segments' in record:
        return _assess_lineage(record)
    return {'cargo_id': record['cargo_id'],
            'result': '封签一致' if seal.unchanged(record) else '封签异常'}


def _assess_lineage(lin):
    if lin['conflicts']:
        result = '无法归属待核'
    elif lin['first_break'] is not None or not lin['signed']:
        result = '未交接断链'
    elif lin['resealed']:
        result = '合法换封后完整交接'
    else:
        result = '原封交接'
    return {'cargo_id': lin['cargo_id'], 'result': result,
            'chain': lin['chain'], 'arrival_port': lin['arrival_port'],
            'first_break': lin['first_break'], 'conflicts': lin['conflicts']}


def assess_all(records):
    """逐箱交接账：对全部采录记录输出 {货箱号: 交接结果}。"""
    return {cargo_id: _assess_lineage(lin)
            for cargo_id, lin in seal.lineages(records).items()}
