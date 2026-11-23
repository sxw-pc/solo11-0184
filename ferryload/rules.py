import hashlib
import json
from collections import defaultdict
from datetime import datetime
from . import tide


def _latest(events, kind, key):
    out = {}
    for e in events:
        if e['kind'] == kind:
            out[e[key]] = e
    return out


def _decision(result, reason, *ids):
    return {'result': result, 'reason': reason, 'evidence': [x for x in ids if x]}


def _instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _tide(v):
    departure = _instant(v['departure_at'])
    windows = v.get('tide_windows', [])
    current = [w for w in windows if _instant(w['start']) <= departure < _instant(w['end']) and w['level_cm'] >= v['required_level_cm']]
    conflict = any(_instant(a['start']) < _instant(b['end']) and _instant(b['start']) < _instant(a['end']) and a['level_cm'] != b['level_cm'] for i, a in enumerate(windows) for b in windows[i + 1:])
    return bool(current) and not conflict, conflict


def _canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


# ---------------------------------------------------------------------------
# 新证据子系统的装配（水尺、通道包络、卸车重配、签名版本）
# ---------------------------------------------------------------------------

def _restow_events(events, voyage_id):
    return sorted((e for e in events if e.get('kind') == 'restow' and e.get('voyage_id') == voyage_id),
                  key=lambda e: e['id'])


def _signed_versions(events, voyage_id):
    """船长对每个装载版本的签认：返回 {load_version: 签名事件}。"""
    out = {}
    for e in events:
        if e.get('kind') == 'captain_sign' and e.get('voyage_id') == voyage_id:
            out[e.get('load_version', 1)] = e
    return out


def _effective_lanes(v, restows):
    """restow 事件可携带经稳性验证的新舱位（如中心线 L3），与原舱道合并。"""
    lanes = dict(v.get('lanes', {}))
    for r in restows:
        for lane_id, spec in r.get('new_lanes', {}).items():
            lanes.setdefault(lane_id, spec)
    return lanes


def _synth_boarding(v, reservations, plans, restows, lanes):
    """按 stow + restow 的时间顺序合成“当前实际登船清单”。

    卸下一辆车必须同时改这里的清单、重量与力臂，而不只是改 required_level_cm。
    """
    actions = []
    for p in plans:
        if p.get('boarded'):
            actions.append((p.get('seq', 0), 'board', p['vin'], p['lane']))
    for r in restows:
        actions.append((r.get('seq', 0), r.get('action', 'restow'), r['vin'], r.get('lane')))
    actions.sort(key=lambda a: a[0])

    on_board = {}
    for _, action, vin, lane in actions:
        if action in ('unload', 'offload'):
            on_board.pop(vin, None)
        else:
            on_board[vin] = lane

    boarding = []
    for vin, lane in on_board.items():
        row = reservations.get(vin)
        if row is None:
            continue
        lever = lanes.get(lane, {}).get('lever_arm_cm', 0)
        boarding.append({'vin': vin, 'lane': lane, 'weight_kg': row.get('measured_kg', 0),
                         'lever_arm_cm': lever, 'load_version': _load_version(restows, plans, vin)})
    return boarding


def _load_version(restows, plans, vin):
    versions = [p.get('version', 1) for p in plans if p.get('vin') == vin]
    versions += [r.get('load_version', 1) for r in restows if r.get('vin') == vin]
    return max(versions) if versions else 1


def _current_load_version(plans, restows):
    versions = [p.get('version', 1) for p in plans] + [r.get('load_version', 1) for r in restows]
    return max(versions) if versions else 1


def _center_of_gravity(boarding):
    weight = sum(float(b['weight_kg']) for b in boarding)
    moment = sum(float(b['weight_kg']) * float(b['lever_arm_cm']) for b in boarding)
    center = moment / weight if weight else 0.0
    return weight, center


def _manifest_check(v, boarding, lanes, reservations):
    """对当前实际登船清单做舱道尺寸/重量与稳性核验。"""
    if not boarding:
        return False, 0, 0.0
    lane_ok = True
    for b in boarding:
        lane = lanes.get(b['lane'])
        row = reservations.get(b['vin'], {})
        if lane is None or b['weight_kg'] > lane['weight_kg']:
            lane_ok = False
        if row.get('length_cm', 0) > lane.get('length_cm', 10 ** 9) or row.get('height_cm', 0) > lane.get('height_cm', 10 ** 9):
            lane_ok = False
    weight, center = _center_of_gravity(boarding)
    stability_ok = v.get('stability_min_cm', -30) <= center <= v.get('stability_max_cm', 30)
    return lane_ok and stability_ok, weight, center


def calculate(events):
    voyages = _latest(events, 'voyage', 'voyage_id')
    reservations = _latest(events, 'vehicle', 'vin')
    stows = _latest(events, 'stow', 'vin')
    channels = _latest(events, 'channel', 'voyage_id')
    gauges_all = [e for e in events if e.get('kind') == 'gauge']
    states = []
    decisions = {}
    certificates = []

    for voyage_id, v in voyages.items():
        vehicle_rows = [x for x in reservations.values() if x['voyage_id'] == voyage_id]
        confirmed_rows = [x for x in vehicle_rows if x.get('status', 'confirmed') == 'confirmed']
        plans = [x for x in stows.values() if x['voyage_id'] == voyage_id]
        tide_ok, tide_conflict = tide.evaluate(v)
        restows = _restow_events(events, voyage_id)
        lanes = _effective_lanes(v, restows)
        boarding = _synth_boarding(v, reservations, plans, restows, lanes)
        load_version = _current_load_version(plans, restows)
        signed = _signed_versions(events, voyage_id)
        # 版本 1 或仅有扫描状态变更（无真正卸车重配）时，回退到航次自身的
        # captain_signed，保持旧 fixture 的既有放行路径；restow 产生的新装载
        # 版本必须有对应版本的独立船长签认。
        if signed and restows:
            signature_ok = load_version in signed and signed[load_version].get('signed', False)
            signature_evidence = [s['id'] for s in signed.values()]
        else:
            signature_ok = bool(v.get('captain_signed', False)) and not restows
            signature_evidence = [v['id']] if v.get('captain_signed') else []

        capacity = sum(l['weight_kg'] for l in v.get('lanes', {}).values())
        weight = sum(x.get('measured_kg', x.get('declared_kg', 0)) for x in confirmed_rows if x.get('weigh_calibrated', False))
        boarded_weight = sum(b['weight_kg'] for b in boarding)
        moment = sum(b['weight_kg'] * b['lever_arm_cm'] for b in boarding)
        center = moment / boarded_weight if boarded_weight else 0
        stability_ok = boarded_weight > 0 and v.get('stability_min_cm', -30) <= center <= v.get('stability_max_cm', 30)
        lane_over = any(p['lane'] not in v.get('lanes', {}) or reservations[p['vin']].get('measured_kg', 0) > v['lanes'][p['lane']]['weight_kg'] for p in plans if p['vin'] in reservations)
        active = not v.get('cancelled', False)

        # --- 连续包络通道裁定（新）：对所有与原件一致的曲线都安全才放行 ---
        channel = channels.get(voyage_id)
        channel_gauges = [g for g in gauges_all if g.get('voyage_id') == voyage_id]
        assessment = None
        envelope_ok = True
        if channel is not None:
            assessment = tide.assess_channel(channel, channel_gauges, v, boarding)
            envelope_ok = assessment['verdict'] == 'safe'

        decisions.update({
            '航次潮窗核定': _decision('开放' if tide_ok and active else '停航', '潮窗有效、无冲突且未撤销' if tide_ok and active else '潮窗冲突/失效或航次停航', v['id']),
            '舱道分区额度': _decision('有余量' if weight <= capacity and not lane_over else '分区超载', '有效实测总重与各舱道额定重量比较', v['id'], *(x['id'] for x in vehicle_rows)),
            '稳性裁定': _decision('平衡' if stability_ok else '失衡或无实载', f'实载总重 {boarded_weight} 千克，横向重心 {center:.1f} 厘米', v['id'], *(p['id'] for p in plans)),
            '潮窗冲突冻结': _decision('冻结' if tide_conflict else '正常', '冲突潮窗不得继续开放', v['id']),
            '停航退票责任': _decision('应退票' if not active and any(not stows.get(x['vin'], {}).get('boarded') for x in vehicle_rows) else '无退票', '只对停航且未实际登船的预约生成责任', v['id'])})
        states.append(('航次与运力账', voyage_id, '开放' if tide_ok and active else '停航', v.get('version', 1)))
        for x in vehicle_rows:
            vin = x['vin']
            matched = x.get('scan_vin') == vin
            measured = x.get('measured_kg') is not None and x.get('weigh_calibrated') and _instant(x['calibration_start']) <= _instant(x['weighed_at']) < _instant(x['calibration_end'])
            lane = stows.get(vin, {}).get('lane')
            neighbors = v.get('adjacent_lanes', {}).get(lane, [])
            haz_neighbors = [other for other in vehicle_rows if other['vin'] != vin and other.get('hazmat_class') and stows.get(other['vin'], {}).get('lane') in neighbors]
            haz = x.get('hazmat_declared', True) and not (x.get('hazmat_class') and haz_neighbors)
            decisions[f'车辆身份复核:{vin}'] = _decision('通过' if matched else '拒载', '预约 VIN 必须与闸口扫描一致', x['id'])
            decisions[f'实测重量裁定:{vin}'] = _decision('有效' if measured else '待复称', '校准有效的实测重量参与全船额度', x['id'])
            decisions[f'危险品隔离核验:{vin}'] = _decision('合格' if haz else '拒载', '危险品须如实申报并隔离', x['id'])
            decisions[f'称重校准可信度:{vin}'] = _decision('可信' if x.get('weigh_calibrated') else '失效', '校准失效的重量不应被签认', x['id'])
            states.append(('车辆申报与验车账', vin, '合格' if matched and measured and haz else '拒载或待核', x.get('version', 1)))
        occupancy = defaultdict(list)
        for p in plans:
            occupancy[p['lane']].append(p)
        valid_plan = all(len(xs) == 1 and xs[0]['vin'] in reservations and xs[0]['lane'] in v.get('lanes', {}) and reservations[xs[0]['vin']].get('measured_kg', 0) <= v['lanes'][xs[0]['lane']]['weight_kg'] and reservations[xs[0]['vin']].get('length_cm', 0) <= v['lanes'][xs[0]['lane']]['length_cm'] and reservations[xs[0]['vin']].get('height_cm', 0) <= v['lanes'][xs[0]['lane']]['height_cm'] for xs in occupancy.values()) and bool(plans) and len(plans) == len(confirmed_rows)
        hazard_conflict = any(x.get('hazmat_class') and any(other['vin'] != x['vin'] and other.get('hazmat_class') and stows.get(other['vin'], {}).get('lane') in v.get('adjacent_lanes', {}).get(stows.get(x['vin'], {}).get('lane'), []) for other in confirmed_rows) for x in confirmed_rows)
        confirmed_ready = all(x.get('scan_vin') == x['vin'] and x.get('weigh_calibrated') and _instant(x['calibration_start']) <= _instant(x['weighed_at']) < _instant(x['calibration_end']) and x.get('hazmat_declared', True) for x in confirmed_rows)
        plan_eligible = valid_plan and tide_ok and active and not lane_over and not hazard_conflict and confirmed_ready
        boarded = all(p.get('boarded') for p in plans)
        scanned = all(p.get('boarded') and p.get('unloaded') for p in plans)
        waitlist = sorted((x for x in vehicle_rows if x['vin'] not in stows and x.get('scan_vin') == x['vin'] and x.get('weigh_calibrated') and _instant(x['calibration_start']) <= _instant(x['weighed_at']) < _instant(x['calibration_end']) and x.get('hazmat_declared', True)),
                          key=lambda x: (-x.get('priority', 0), x['id']))
        occupied_lanes = {p['lane'] for p in plans if p.get('boarded')}
        free_lanes = set(v.get('lanes', {})) - occupied_lanes

        def candidate_fits(x, lane_id):
            lane = v['lanes'][lane_id]
            size_ok = x.get('measured_kg', 0) <= lane['weight_kg'] and x.get('length_cm', 0) <= lane['length_cm'] and x.get('height_cm', 0) <= lane['height_cm']
            neighbors = v.get('adjacent_lanes', {}).get(lane_id, [])
            hazard_ok = not x.get('hazmat_class') or not any(reservations[p['vin']].get('hazmat_class') and p['lane'] in neighbors for p in plans if p.get('boarded') and p['vin'] in reservations)
            return size_ok and hazard_ok
        eligible_candidate = next((x for x in waitlist if any(candidate_fits(x, lane) for lane in free_lanes)), None)
        vehicles_ok = bool(confirmed_rows) and all(x.get('scan_vin') == x['vin'] and x.get('weigh_calibrated') and _instant(x['calibration_start']) <= _instant(x['weighed_at']) < _instant(x['calibration_end']) and x.get('hazmat_declared', True) and not (x.get('hazmat_class') and any(other['vin'] != x['vin'] and other.get('hazmat_class') and stows.get(other['vin'], {}).get('lane') in v.get('adjacent_lanes', {}).get(stows.get(x['vin'], {}).get('lane'), []) for other in confirmed_rows)) for x in confirmed_rows)
        departures = active and tide_ok and weight <= capacity and stability_ok and vehicles_ok and valid_plan and boarded and v.get('captain_signed', False)

        # --- 连续包络（非插值）冻结裁定 ---
        if channel is not None:
            verdict = assessment['verdict']
            if verdict == 'inconsistent':
                chan_result, chan_reason = '冻结待补测', '水尺原件与授权包络互相矛盾：' + ','.join(assessment['inconsistent_gauges'])
            elif verdict == 'frozen':
                md = assessment['most_dangerous']
                chan_result, chan_reason = ('冻结待补测',
                                            f"最危险时刻 {md['at']} 可行水位 {md['min_feasible_cm']}–{md['max_feasible_cm']} cm，"
                                            f"需 {md['required_cm']} cm，最坏缺 {-md['min_margin_cm']:.1f} cm；连续包络存在未测低谷")
            else:
                md = assessment['most_dangerous']
                chan_result, chan_reason = ('全曲线安全',
                                            f"全程最低可行 {assessment['overall_min_feasible_cm']} cm >= 需求 {md['required_cm']} cm，"
                                            f"对所有与原件一致的曲线均安全")
            decisions['浅滩连续包络裁定'] = _decision(chan_result, chan_reason, channel['id'], *assessment['gauges_used'])
            decisions['包络冻结恢复'] = _decision(
                '可补测或卸车' if verdict != 'safe' else '无需恢复',
                '路线甲：中途增派独立水尺并验证无未测低谷；路线乙：卸下较重车辆并在已验证稳性舱位重排，降低吃水',
                channel['id'], *(r['id'] for r in restows))
        else:
            verdict = None

        # --- 卸车重配：清单、吃水、稳性、候补同时重算 ---
        manifest_ok, manifest_weight, manifest_center = _manifest_check(v, boarding, lanes, reservations)
        if restows:
            r0 = restows[-1]
            removed = [r['vin'] for r in restows if r.get('action') in ('unload', 'offload')]
            moved = [(r['vin'], r.get('lane')) for r in restows if r.get('action') == 'restow']
            new_draft = tide.hull_draft_cm(v, manifest_weight)
            decisions['卸车重配清单'] = _decision(
                '重配完成' if manifest_ok else '重配不稳',
                f"装载版本 {load_version}：实载 {manifest_weight} 千克、重心 {manifest_center:.1f} 厘米、吃水 {new_draft:.1f} cm；"
                f"卸下 {removed or '无'}，重排 {moved or '无'}",
                *(r['id'] for r in restows))
            rollover = [r for r in restows if r.get('next_voyage_id')]
            decisions['原班候补改派'] = _decision(
                '重车进入下一班' if rollover else '无改派',
                '卸下车辆按原班候补登记到下一航次，原班只运余车',
                *(r['id'] for r in rollover))
            states.append(('配载与航行账', voyage_id, f'重配版本{load_version}', load_version))
            # 重配后的离港必须通过新清单的稳性/舱位硬约束
            departures = departures and manifest_ok
        else:
            new_draft = tide.hull_draft_cm(v, boarded_weight)

        # --- 签名版本：每个装载版本需要对应版本的船长签认 ---
        decisions['船长签名版本'] = _decision(
            '已签认' if signature_ok else '待签认',
            f"当前装载版本 {load_version}，已签认版本 {sorted(signed)}",
            *signature_evidence)

        departures = departures and envelope_ok and signature_ok
        decisions['舱位配载确认'] = _decision('确认' if plan_eligible else '待改配', '舱位唯一且航次潮窗、额度和状态仍有效', v['id'], *(p['id'] for p in plans))
        decisions['登船与离船闭环'] = _decision('闭环' if scanned else '缺口', '每辆已配载车应有登船及到港离船扫描', *(p['id'] for p in plans), v['id'])
        decisions['离港放行裁定'] = _decision('放行' if departures else '阻断',
                                              '潮窗、连续包络、重量、配载、扫描和分版本船长签认共同决定',
                                              v['id'], *(p['id'] for p in plans), *[g['gauge_id'] for g in channel_gauges])
        wait_result = ('可递补:' + eligible_candidate['vin']) if eligible_candidate else (('候补待空位:' + waitlist[0]['vin']) if waitlist and not free_lanes else ('无合格候补' if waitlist else '无候补'))
        decisions['候补递补资格'] = _decision(wait_result, '合格而未占舱位的预约按优先级、舱道尺寸和危险品隔离递补', v['id'], *(x['id'] for x in waitlist))
        decisions['实载额度释放'] = _decision(f'{len(free_lanes)}条空舱道', '只将已经登船的车辆计入实载占用；缺扫描的原计划释放可用额度', v['id'], *(p['id'] for p in plans))
        for p in plans:
            states.append(('配载与航行账', p['vin'], ('完成' if p.get('boarded') and p.get('unloaded') else '待闭环') if plan_eligible else '待改配', p.get('version', 1)))

        # --- 放行证书：按装载版本固化输入指纹与曲线见证，append-only 审计 ---
        if channel is not None:
            evidence_ids = [v['id'], channel['id']] + [g['gauge_id'] for g in channel_gauges] + \
                           [p['id'] for p in plans] + [r['id'] for r in restows] + \
                           [s['id'] for s in signed.values()] + [x['id'] for x in confirmed_rows]
            fingerprint = hashlib.sha256(_canonical({
                'voyage': v, 'channel': channel,
                'gauges': sorted(channel_gauges, key=lambda g: g['gauge_id']),
                'boarding': sorted(boarding, key=lambda b: b['vin']),
                'load_version': load_version,
            }).encode('utf-8')).hexdigest()
            md = assessment['most_dangerous'] or {}
            certificates.append({
                'voyage_id': voyage_id,
                'load_version': load_version,
                'ruling': '放行' if departures else ('冻结待补测' if verdict != 'safe' else '阻断'),
                'draft_cm': round(new_draft, 2),
                'verdict': verdict,
                'required_level_cm': assessment['required_level_cm'],
                'most_dangerous_at': md.get('at'),
                'min_feasible_cm': md.get('min_feasible_cm'),
                'max_feasible_cm': md.get('max_feasible_cm'),
                'input_fingerprint': fingerprint,
                'manifest': sorted(b['vin'] for b in boarding),
                'witness_low': assessment['witness_low_curve'],
                'witness_high': assessment['witness_high_curve'],
                'evidence': evidence_ids,
                'signed_version_ok': signature_ok,
            })
    for vin, x in reservations.items():
        if x['voyage_id'] in voyages:
            continue
        matched = x.get('scan_vin') == vin
        measured = x.get('measured_kg') is not None and x.get('weigh_calibrated') and _instant(x['calibration_start']) <= _instant(x['weighed_at']) < _instant(x['calibration_end'])
        decisions[f'车辆身份复核:{vin}'] = _decision('通过' if matched else '拒载', '预约 VIN 与闸口扫描独立核验；航次证据尚未导入', x['id'])
        decisions[f'实测重量裁定:{vin}'] = _decision('有效' if measured else '待复称', '独立校准凭据覆盖称重时刻', x['id'])
        decisions[f'危险品隔离核验:{vin}'] = _decision('待配载' if x.get('hazmat_class') else '合格', '危险品邻接要在实际配载后才能核定', x['id'])
        decisions[f'称重校准可信度:{vin}'] = _decision('可信' if measured else '失效', '称重凭据单独留存', x['id'])
        states.append(('车辆申报与验车账', vin, '合格' if matched and measured else '待核', x.get('version', 1)))
    for vin, p in stows.items():
        if p['voyage_id'] in voyages:
            continue
        decisions[f'登船与离船闭环:{vin}'] = _decision('闭环' if p.get('boarded') and p.get('unloaded') else '缺口', '登离船扫描可独立登记，最终放行待航次证据', p['id'])
        states.append(('配载与航行账', vin, '待航次核定', p.get('version', 1)))
    return decisions, states, certificates


RECORD_SPECS = {
    'voyage': ('航次与运力账', 'voyage_id', [('航次版本', ['tide_valid', 'tide_conflict', 'cancelled', 'captain_signed']), ('舱道额度', ['lanes'])]),
    'vehicle': ('车辆申报与验车账', 'vin', [('预约单', ['voyage_id', 'vin', 'declared_kg']), ('验车记录', ['scan_vin', 'measured_kg', 'weigh_calibrated', 'hazmat_declared'])]),
    'stow': ('配载与航行账', 'vin', [('配载方案', ['voyage_id', 'lane']), ('登离船扫描', ['boarded', 'unloaded'])]),
    'gauge': ('水尺采录账', 'gauge_id', [('水尺原件', ['voyage_id', 'observed_at', 'level_cm', 'calibrated'])]),
    'channel': ('通道包络账', 'voyage_id', [('物理包络', ['envelope', 'speed_kn', 'net_clearance_cm', 'distance_nm'])]),
    'restow': ('配载与航行账', 'vin', [('卸车重配', ['voyage_id', 'action', 'lane', 'load_version', 'next_voyage_id'])]),
    'captain_sign': ('船长签认账', 'voyage_id', [('签名版本', ['load_version', 'signed', 'signed_at'])]),
}


def business_records(event):
    subsystem, key, specs = RECORD_SPECS[event['kind']]
    return [(subsystem, name, str(event[key]), {field: event.get(field) for field in fields}) for name, fields in specs]
