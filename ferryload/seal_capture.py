"""封签采录：出发、海关、到达三站的纸面登记与照片 OCR 录入。

每条记录保存货箱号、VIN、口岸、经办交接事件、原封签实体号、拆封原因、
新封签发行流水、发生时间、录入时间、页码和撤销/勘误指向。
"""
import json
from datetime import datetime
from pathlib import Path

KINDS = ('departure', 'unseal', 'reseal', 'arrival', 'transfer', 'revocation')

# 各记录类型在采录时必须补齐的字段；查验单号缺失不在此拦截，留给谱系判定断点
REQUIRED = {
    'departure': ('new_seal', 'port'),
    'unseal': ('old_seal', 'reason', 'port'),
    'reseal': ('new_seal', 'issuance', 'port'),
    'arrival': ('old_seal', 'port'),
    'transfer': ('old_seal', 'port'),
    'revocation': ('revokes',),
}

DEFAULTS = {'vin': None, 'port': None, 'event': None, 'old_seal': None, 'reason': None,
            'issuance': None, 'new_seal': None, 'page': None, 'source': 'registry',
            'revokes': None}


def _check_instant(value, field):
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        raise ValueError(f'{field} 时间格式非法: {value!r}')


def normalize(raw):
    """把一条原始登记/照片 OCR 规范化为标准采录记录。"""
    record = dict(raw)
    kind = record.get('kind')
    if kind not in KINDS:
        raise ValueError(f'未知记录类型: {kind!r}')
    if not record.get('record_id'):
        raise ValueError('缺少记录号 record_id')
    for field in REQUIRED[kind]:
        if record.get(field) in (None, ''):
            raise ValueError(f'{kind} 记录缺少字段: {field}')
    if kind != 'revocation':
        if not record.get('cargo_id'):
            raise ValueError('缺少货箱号 cargo_id')
        if not record.get('occurred_at'):
            raise ValueError('缺少发生时间 occurred_at')
        record['recorded_at'] = record.get('recorded_at') or record['occurred_at']
        _check_instant(record['occurred_at'], 'occurred_at')
        _check_instant(record['recorded_at'], 'recorded_at')
    for field, default in DEFAULTS.items():
        record.setdefault(field, default)
    return record


def capture(raw_records):
    """批量采录：逐条规范化，并校验记录号唯一、撤销/勘误指向闭合。"""
    records = [normalize(raw) for raw in raw_records]
    seen = set()
    for record in records:
        rid = record['record_id']
        if rid in seen:
            raise ValueError(f'记录号重复: {rid}')
        seen.add(rid)
    for record in records:
        target = record.get('revokes')
        if target is not None:
            if target == record['record_id']:
                raise ValueError(f'记录不能撤销自身: {target}')
            if target not in seen:
                raise ValueError(f'撤销/勘误指向未知记录: {target}')
    return records


def load(source):
    """从路径或已解析 JSON（记录列表或含 records 的对象）读入采录记录。"""
    if isinstance(source, (str, Path)):
        raw = json.loads(Path(source).read_text(encoding='utf-8'))
    else:
        raw = source
    if isinstance(raw, dict):
        raw = raw['records']
    return capture(raw)
