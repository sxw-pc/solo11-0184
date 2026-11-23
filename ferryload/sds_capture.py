"""SDS 安全数据表离线采录与多元禁配超边图。

港务收到的安全数据表（SDS）以合成 PDF/OCR token 形式离线采录：每份文件记录
化学品代码、货物批次、危险条件的适用温度与运输方式、来源页码与勘误版本。
一条限制不会被拆成若干条不存在的两两冲突，而是映射为一条由车辆身份、货物
批次与通风分区共同限定的禁配超边；只有超边全部成员同时落在同一通风分区且
危险条件适用时才构成禁配。未在两两表中出现，不得作为多元组合安全的依据。

数据质量存疑（OCR 丢失否定词、旧 SDS 页混入新勘误、同一货物名对应多个浓度、
车辆换货后仍沿用旧批次）的限制标记为「待核」：输出明确原因交人工复核，不
参与自动阻断；确认命中的超边输出「禁配」并给出最小冲突集，交给
ferryload/stow.py 的方案资格与 ferryload/rules.py 的配载确认、危险品隔离和
离港名单阻断。迟到的勘误以新版本采录，旧版来源文句保留在图中供追溯重算，
不得以最终版覆盖当时的来源文句。
"""

NEGATION_TOKENS = ('不得', '禁止', '禁运', '勿', '不', '禁', 'never', 'not')


def _zone_map(voyage):
    """通风分区 → 舱道的反查表；未划区的舱道视为各自独立分区。"""
    zmap = {}
    for zone, lanes in (voyage.get('ventilation_zones') or {}).items():
        for lane in lanes:
            zmap[lane] = zone
    return zmap


def _zone_of(lane, zmap):
    return zmap.get(lane, f'未分区:{lane}')


def _cargo_matches(vehicle_cargo, restriction_cargo):
    """车辆实载货物是否命中限制成员：化学品代码必等，批次与浓度按采录值限定。"""
    if not isinstance(vehicle_cargo, dict):
        return False
    if vehicle_cargo.get('chemical_code') != restriction_cargo.get('chemical_code'):
        return False
    if restriction_cargo.get('batch') and vehicle_cargo.get('batch') != restriction_cargo.get('batch'):
        return False
    if (restriction_cargo.get('concentration')
            and vehicle_cargo.get('concentration') != restriction_cargo.get('concentration')):
        return False
    return True


def _condition_applies(condition, voyage):
    """危险条件的适用温度与运输方式同时满足时限制才生效。"""
    if not condition:
        return True
    modes = condition.get('transport_modes')
    if modes and voyage.get('transport_mode', 'sea') not in modes:
        return False
    temp = voyage.get('forecast_temp_c')
    if temp is not None:
        if condition.get('temp_min_c') is not None and temp < condition['temp_min_c']:
            return False
        if condition.get('temp_max_c') is not None and temp > condition['temp_max_c']:
            return False
    return True


def build_graph(events, vehicles=None):
    """把 SDS 采录事件构建为禁配超边图。

    返回 {'documents', 'restrictions', 'findings'}：restrictions 为超边
    （成员货物、适用条件、来源文句、页码、勘误版本、是否存疑），findings
    为需要人工复核的待核项。vehicles 用于发现换货后仍沿用旧批次的情况。
    """
    documents = []
    restrictions = []
    findings = []
    for event in events:
        if event.get('kind') != 'sds':
            continue
        doc_id = event.get('doc_id', event['id'])
        doc_version = event.get('errata_version', 1)
        pages = event.get('pages', [])
        doc_restrictions = event.get('restrictions', [])
        documents.append({'event_id': event['id'], 'doc_id': doc_id,
                          'errata_version': doc_version})
        # OCR 丢失否定词：校对原文中的否定词在 token 流里不复存在。
        for page in pages:
            proof = page.get('proofread', '')
            joined = ''.join(page.get('tokens', []))
            missing = [neg for neg in NEGATION_TOKENS if neg in proof and neg not in joined]
            if missing:
                findings.append({
                    'code': 'ocr_negation_missing', 'event_id': event['id'], 'doc_id': doc_id,
                    'page': page.get('page'),
                    'source_pages': [page.get('page')],
                    'detail': (f"SDS {doc_id} 第{page.get('page')}页 OCR 丢失否定词"
                               f"{'、'.join(missing)}，原文「{proof}」语义可能反转，待人工核对")})
        # 旧 SDS 页与新勘误混用：同一文件内页码版本与文件勘误版本不一致。
        mixed_pages = [p.get('page') for p in pages
                       if p.get('errata', doc_version) != doc_version]
        if mixed_pages:
            findings.append({
                'code': 'errata_mixed', 'event_id': event['id'], 'doc_id': doc_id,
                'source_pages': mixed_pages,
                'detail': (f"SDS {doc_id} 勘误v{doc_version} 混入旧版页{mixed_pages}，"
                           f"旧 SDS 页与新勘误不得混用，待人工核对")})
        # 同一货物名对应不同浓度：目录里同名货物有多个浓度而限制未指明。
        by_name = {}
        for item in event.get('catalog', []):
            by_name.setdefault(item.get('name'), set()).add(item.get('concentration'))
        for order, restriction in enumerate(doc_restrictions):
            rid = restriction.get('id') or f'{doc_id}#{order}'
            for cargo in restriction.get('cargoes', []):
                options = by_name.get(cargo.get('name'), set())
                if not cargo.get('concentration') and len(options) > 1:
                    findings.append({
                        'code': 'concentration_ambiguous', 'event_id': event['id'],
                        'doc_id': doc_id, 'restriction_id': rid,
                        'detail': (f"货物名「{cargo.get('name')}」对应多个浓度"
                                   f"{sorted(options)}，限制 {rid} 未指明浓度，"
                                   f"无法判定适用批次，待人工核对")})
            restrictions.append({
                'event_id': event['id'], 'doc_id': doc_id, 'id': rid,
                'scope': restriction.get('scope', 'ventilation_zone'),
                'cargoes': restriction.get('cargoes', []),
                'condition': restriction.get('condition') or {},
                'text': restriction.get('text', ''),
                'source_pages': restriction.get('source_pages', []),
                'errata_version': restriction.get('errata_version', doc_version)})
    # 车辆换货后仍用旧批次：车辆实载批次与限制采录批次不一致。
    for restriction in restrictions:
        for cargo in restriction['cargoes']:
            for x in vehicles or []:
                current = x.get('cargo')
                if not isinstance(current, dict):
                    continue
                if (current.get('chemical_code') == cargo.get('chemical_code')
                        and current.get('concentration') == cargo.get('concentration')
                        and current.get('batch') and cargo.get('batch')
                        and current['batch'] != cargo['batch']):
                    findings.append({
                        'code': 'batch_stale', 'event_id': restriction['event_id'],
                        'doc_id': restriction['doc_id'], 'restriction_id': restriction['id'],
                        'vin': x['vin'],
                        'detail': (f"车辆 {x['vin']} 已换货为批次 {current['batch']}，"
                                   f"SDS 限制 {restriction['id']} 仍按旧批次 {cargo['batch']} "
                                   f"采录，须重新采录核对，待人工确认")})
    # 关联待核项 → 超边，标存疑；并记录涉及批次供按航次过滤。
    by_doc = {}
    for restriction in restrictions:
        by_doc.setdefault(restriction['doc_id'], []).append(restriction)
    for finding in findings:
        linked = [r for r in by_doc.get(finding['doc_id'], [])
                  if r['id'] == finding.get('restriction_id')
                  or (finding.get('restriction_id') is None
                      and set(r['source_pages']) & set(finding.get('source_pages', [])))]
        finding['restriction_ids'] = [r['id'] for r in linked]
        finding['batches'] = {c.get('batch') for r in linked for c in r['cargoes']
                              if c.get('batch')}
    suspect_ids = {rid for finding in findings for rid in finding['restriction_ids']}
    for restriction in restrictions:
        restriction['suspect'] = restriction['id'] in suspect_ids
    return {'documents': documents, 'restrictions': restrictions, 'findings': findings}


def _active_restrictions(graph, errata_version=None):
    """同一限制血脉（doc_id+限制号）只取不超过指定勘误版本的最新一条。"""
    active = {}
    for restriction in graph['restrictions']:
        if errata_version is not None and restriction['errata_version'] > errata_version:
            continue
        key = (restriction['doc_id'], restriction['id'])
        if key not in active or restriction['errata_version'] > active[key]['errata_version']:
            active[key] = restriction
    return list(active.values())


def violations(graph, plans, vehicles, voyage, errata_version=None):
    """某张配载表命中的多元限制：返回禁配超边及最小冲突集。

    超边全部成员货物同处一个通风分区且危险条件适用时命中；conflict 为最小
    冲突集（每个成员货物各取一辆实载车），不得以两两邻接无冲突解释。存疑
    限制（suspect=True）的命中只作待核提示，不参与自动阻断。
    """
    if not graph:
        return []
    zmap = _zone_map(voyage)
    adjacent = voyage.get('adjacent_lanes', {})
    occupancy = {}
    for plan in plans:
        vin = plan.get('vin')
        if vin not in vehicles:
            continue
        zone = _zone_of(plan.get('lane'), zmap)
        occupancy.setdefault(zone, []).append((vin, plan.get('lane')))
    hits = []
    for restriction in _active_restrictions(graph, errata_version):
        if restriction['scope'] != 'ventilation_zone':
            continue
        if not _condition_applies(restriction['condition'], voyage):
            continue
        for zone, occupants in occupancy.items():
            conflict = []
            carriers = {}
            for cargo in restriction['cargoes']:
                matched = sorted(vin for vin, _ in occupants
                                 if _cargo_matches(vehicles[vin].get('cargo'), cargo))
                if not matched:
                    break
                carriers[(cargo.get('chemical_code'), cargo.get('batch'))] = matched
                conflict.append(matched[0])
            else:
                lanes = sorted(lane for vin, lane in occupants if vin in conflict)
                non_adjacent = (len(lanes) > 1 and all(
                    other not in adjacent.get(lane, [])
                    for index, lane in enumerate(lanes) for other in lanes[index + 1:]))
                hits.append({
                    'restriction_id': restriction['id'], 'doc_id': restriction['doc_id'],
                    'event_id': restriction['event_id'], 'zone': zone,
                    'conflict': sorted(conflict), 'carriers': carriers,
                    'batches': sorted(c.get('batch') for c in restriction['cargoes']
                                      if c.get('batch')),
                    'lanes': lanes, 'non_adjacent': non_adjacent,
                    'text': restriction['text'], 'source_pages': restriction['source_pages'],
                    'errata_version': restriction['errata_version'],
                    'suspect': restriction['suspect']})
    return hits


def resolutions(graph, hit, plans, vehicles, voyage):
    """消除一条命中超边的合法操作：换区改配或下船/退候补。

    换区只考虑未被占用、尺寸可容纳且改配后不再命中任何超边的舱道；下船
    （退到候补/下一航次）对最小冲突集任一辆车都成立。
    """
    lanes = voyage.get('lanes', {})
    zmap = _zone_map(voyage)
    occupied = {p['lane'] for p in plans}
    moves = []
    offloads = []
    for vin in hit['conflict']:
        offloads.append({'vin': vin, 'action': 'offload',
                         'detail': (f"{vin} 下船或退到候补/下一航次即拆除超边 "
                                    f"{hit['restriction_id']} 的最小冲突集")})
        current = next((p for p in plans if p['vin'] == vin), None)
        if not current:
            continue
        vehicle = vehicles.get(vin, {})
        for lane_id in sorted(lanes):
            if lane_id in occupied or lane_id == current['lane']:
                continue
            zone = _zone_of(lane_id, zmap)
            if zone == hit['zone']:
                continue
            lane = lanes[lane_id]
            if not (vehicle.get('measured_kg', 0) <= lane['weight_kg']
                    and vehicle.get('length_cm', 0) <= lane['length_cm']
                    and vehicle.get('height_cm', 0) <= lane['height_cm']):
                continue
            hypo = [dict(p, lane=lane_id) if p['vin'] == vin else p for p in plans]
            if any(not h['suspect'] for h in violations(graph, hypo, vehicles, voyage)):
                continue
            moves.append({'vin': vin, 'action': 'move', 'from_lane': current['lane'],
                          'to_lane': lane_id, 'to_zone': zone,
                          'detail': (f"{vin} 改配 {lane_id}（{zone}）后超边 "
                                     f"{hit['restriction_id']} 不再命中")})
    return {'moves': moves, 'offloads': offloads}


def errata_timeline(graph):
    """同一限制血脉的各勘误版本来源文句，按版本留痕供追溯重算。"""
    timeline = {}
    for restriction in graph['restrictions']:
        key = (restriction['doc_id'], restriction['id'])
        timeline.setdefault(key, []).append({
            'errata_version': restriction['errata_version'],
            'text': restriction['text'], 'event_id': restriction['event_id'],
            'source_pages': restriction['source_pages']})
    return {key: sorted(rows, key=lambda row: row['errata_version'])
            for key, rows in timeline.items()}
