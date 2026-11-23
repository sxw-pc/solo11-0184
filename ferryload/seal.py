"""封签谱系：逐箱跟踪有效封签段、换封链、首个断点与跨箱发行流水冲突。

输入为 seal_capture 采录的记录。每次拆封必须消耗当前有效封签，并由同次
查验发出一个未被别箱占用的新实体号；发行流水与封签实体跨箱唯一消耗。
不能只按首尾封签字符串相等与否裁决。
"""


def unchanged(record):
    """旧版单段记录：出发与到达封签字符串相同即为一致。"""
    return record['origin_seal'] == record['arrival_seal']


def _endpoint(record):
    return {'record': record['record_id'], 'port': record.get('port'),
            'event': record.get('event'), 'occurred_at': record.get('occurred_at')}


def _active(records):
    """剔除被撤销/勘误的记录及撤销单本身。"""
    revoked = {r['revokes'] for r in records if r.get('revokes')}
    return [r for r in records
            if r['record_id'] not in revoked and r['kind'] != 'revocation']


def _ordered(records):
    """按发生时间排序（录入时间、记录号兜底），上传顺序不影响谱系。"""
    return sorted(records,
                  key=lambda r: (r['occurred_at'], r['recorded_at'], r['record_id']))


def reused_issuances(records):
    """被多于一条施封记录消耗的发行流水（跨箱全局唯一消耗检查）。"""
    seen, reused = {}, []
    for record in _ordered(_active(records)):
        if record['kind'] != 'reseal':
            continue
        issuance = record['issuance']
        if issuance in seen:
            if issuance not in reused:
                reused.append(issuance)
        else:
            seen[issuance] = record['record_id']
    return reused


def _chain(cargo_id):
    return {'cargo_id': cargo_id, 'segments': [], 'open': None, 'current': None,
            'pending_unseal': None, 'first_break': None, 'conflicts': [],
            'signed': False, 'arrival_port': None, 'resealed': False, 'broken': False}


def _break(chain, record, reason):
    chain['broken'] = True
    if chain['first_break'] is None:
        chain['first_break'] = {'record': record['record_id'], 'reason': reason,
                                'port': record.get('port'),
                                'occurred_at': record.get('occurred_at')}


def _close_segment(chain, record, signed):
    segment = chain['open']
    segment['to'] = _endpoint(record)
    segment['signed'] = signed
    chain['segments'].append(segment)
    chain['open'] = None


def lineages(records):
    """对全部货箱构建封签谱系，返回 {货箱号: 谱系}。

    谱系含每段有效封签（segments）、替换链（chain）、首个断点
    （first_break）与归属冲突（conflicts，含重复使用的发行流水）。
    """
    active = _active(records)
    chains = {}
    for record in _ordered(active):
        chains.setdefault(record['cargo_id'], _chain(record['cargo_id']))
    issuance_owner = {}  # 发行流水 -> 首条消耗记录
    seal_origin = {}     # 封签实体号 -> 首次发行记录
    seal_holder = {}     # 封签实体号 -> 当前持箱
    for record in _ordered(active):
        kind, cargo_id = record['kind'], record['cargo_id']
        chain = chains[cargo_id]
        conflicts_before = len(chain['conflicts'])
        # 跨箱全局检查：即使该箱链已断，仍登记来源与占用冲突
        if kind in ('departure', 'reseal'):
            seal_id = record['new_seal']
            if seal_id in seal_origin:
                chain['conflicts'].append({
                    'kind': '封签实体重复发行', 'seal': seal_id,
                    'record': record['record_id'],
                    'first': seal_origin[seal_id]['record_id']})
            else:
                seal_origin[seal_id] = record
            holder = seal_holder.get(seal_id)
            if holder is not None and holder != cargo_id:
                chain['conflicts'].append({
                    'kind': '封签实体被别箱占用', 'seal': seal_id,
                    'record': record['record_id'], 'holder': holder})
        if kind == 'reseal':
            issuance = record['issuance']
            if issuance in issuance_owner:
                first = issuance_owner[issuance]
                chain['conflicts'].append({
                    'kind': '发行流水重复', 'issuance': issuance,
                    'record': record['record_id'], 'first': first['record_id'],
                    'first_cargo': first['cargo_id']})
            else:
                issuance_owner[issuance] = record
        if kind in ('unseal', 'arrival', 'transfer') and record['old_seal'] not in seal_origin:
            chain['conflicts'].append({
                'kind': '封签来源不明', 'seal': record['old_seal'],
                'record': record['record_id']})
        has_new_conflict = len(chain['conflicts']) > conflicts_before
        if chain['broken']:
            continue
        if kind == 'departure':
            if chain['current'] is not None:
                _break(chain, record, '重复出发登记')
            else:
                chain['current'] = record['new_seal']
                chain['open'] = {'seal': record['new_seal'],
                                 'from': _endpoint(record), 'checkpoints': []}
                seal_holder[record['new_seal']] = cargo_id
        elif kind == 'unseal':
            if not record.get('event'):
                _break(chain, record, '拆封缺少查验单')
            elif chain['current'] is None or record['old_seal'] != chain['current']:
                _break(chain, record, '拆封对象与当前有效封签不符')
            else:
                _close_segment(chain, record, signed=False)
                seal_holder.pop(record['old_seal'], None)
                chain['current'] = None
                chain['pending_unseal'] = record
        elif kind == 'reseal':
            if has_new_conflict:
                _break(chain, record, '施封归属冲突')
            elif chain['pending_unseal'] is None:
                _break(chain, record, '施封缺少同次查验拆封')
            elif not record.get('event') or record['event'] != chain['pending_unseal'].get('event'):
                _break(chain, record, '施封与拆封不属同次查验')
            else:
                chain['current'] = record['new_seal']
                chain['open'] = {'seal': record['new_seal'],
                                 'from': _endpoint(record), 'checkpoints': []}
                seal_holder[record['new_seal']] = cargo_id
                chain['pending_unseal'] = None
                chain['resealed'] = True
        elif kind == 'transfer':
            if chain['current'] is None or record['old_seal'] != chain['current']:
                _break(chain, record, '转运观察与当前有效封签不符')
            else:
                chain['open']['checkpoints'].append(_endpoint(record))
        elif kind == 'arrival':
            if chain['current'] is None:
                _break(chain, record, '到达时无有效封签')
            elif record['old_seal'] != chain['current']:
                _break(chain, record, '到达封签与当前有效封签不符')
            else:
                _close_segment(chain, record, signed=True)
                seal_holder.pop(record['old_seal'], None)
                chain['current'] = None
                chain['signed'] = True
                chain['arrival_port'] = record['port']
    return {cargo_id: _finalize(chain) for cargo_id, chain in chains.items()}


def _finalize(chain):
    segments = list(chain['segments'])
    if chain['open'] is not None:
        segment = chain['open']
        segment['to'] = None
        segment['signed'] = False
        segments.append(segment)
    return {'cargo_id': chain['cargo_id'],
            'segments': segments,
            'chain': [segment['seal'] for segment in segments],
            'first_break': chain['first_break'],
            'conflicts': chain['conflicts'],
            'signed': chain['signed'],
            'arrival_port': chain['arrival_port'],
            'resealed': chain['resealed']}


def lineage(records, cargo_id):
    """单箱谱系便捷入口。"""
    return lineages(records)[cargo_id]
