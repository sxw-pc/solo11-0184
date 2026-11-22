from collections import defaultdict
from datetime import datetime

def _latest(events,kind,key):
    out={}
    for e in events:
        if e['kind']==kind: out[e[key]]=e
    return out

def _decision(result,reason,*ids): return {'result':result,'reason':reason,'evidence':[x for x in ids if x]}

def _instant(value):
    return datetime.fromisoformat(value.replace('Z','+00:00'))

def _tide(v):
    departure=_instant(v['departure_at'])
    windows=v.get('tide_windows',[])
    current=[w for w in windows if _instant(w['start'])<=departure<_instant(w['end']) and w['level_cm']>=v['required_level_cm']]
    conflict=any(_instant(a['start'])<_instant(b['end']) and _instant(b['start'])<_instant(a['end']) and a['level_cm']!=b['level_cm'] for i,a in enumerate(windows) for b in windows[i+1:])
    return bool(current) and not conflict,conflict

def calculate(events):
    voyages=_latest(events,'voyage','voyage_id')
    reservations=_latest(events,'vehicle','vin')
    stows=_latest(events,'stow','vin')
    states=[]; decisions={}
    for voyage_id,v in voyages.items():
        vehicle_rows=[x for x in reservations.values() if x['voyage_id']==voyage_id]
        confirmed_rows=[x for x in vehicle_rows if x.get('status','confirmed')=='confirmed']
        plans=[x for x in stows.values() if x['voyage_id']==voyage_id]
        tide_ok,tide_conflict=_tide(v)
        capacity=sum(l['weight_kg'] for l in v.get('lanes',{}).values())
        weight=sum(x.get('measured_kg',x.get('declared_kg',0)) for x in confirmed_rows if x.get('weigh_calibrated',False))
        active=not v.get('cancelled',False)
        decisions.update({
            '航次潮窗核定':_decision('开放' if tide_ok and active else '停航','潮窗有效、无冲突且未撤销' if tide_ok and active else '潮窗冲突/失效或航次停航',v['id']),
            '舱道分区额度':_decision('有余量' if weight<=capacity else '超载','有效实测总重与各舱道额定重量比较',v['id'],*(x['id'] for x in vehicle_rows)),
            '潮窗冲突冻结':_decision('冻结' if tide_conflict else '正常','冲突潮窗不得继续开放',v['id']),
            '停航退票责任':_decision('应退票' if not active and any(not stows.get(x['vin'],{}).get('boarded') for x in vehicle_rows) else '无退票','只对停航且未实际登船的预约生成责任',v['id'])})
        states.append(('航次与运力账',voyage_id,'开放' if tide_ok and active else '停航',v.get('version',1)))
        for x in vehicle_rows:
            vin=x['vin']; matched=x.get('scan_vin')==vin
            measured=x.get('measured_kg') is not None and x.get('weigh_calibrated') and _instant(x['calibration_start'])<=_instant(x['weighed_at'])<_instant(x['calibration_end'])
            lane=stows.get(vin,{}).get('lane')
            neighbors=v.get('adjacent_lanes',{}).get(lane,[])
            haz_neighbors=[other for other in vehicle_rows if other['vin']!=vin and other.get('hazmat_class') and stows.get(other['vin'],{}).get('lane') in neighbors]
            haz=x.get('hazmat_declared',True) and not (x.get('hazmat_class') and haz_neighbors)
            decisions[f'车辆身份复核:{vin}']=_decision('通过' if matched else '拒载','预约 VIN 必须与闸口扫描一致',x['id'])
            decisions[f'实测重量裁定:{vin}']=_decision('有效' if measured else '待复称','校准有效的实测重量参与全船额度',x['id'])
            decisions[f'危险品隔离核验:{vin}']=_decision('合格' if haz else '拒载','危险品须如实申报并隔离',x['id'])
            decisions[f'称重校准可信度:{vin}']=_decision('可信' if x.get('weigh_calibrated') else '失效','校准失效的重量不应被签认',x['id'])
            states.append(('车辆申报与验车账',vin,'合格' if matched and measured and haz else '拒载或待核',x.get('version',1)))
        occupancy=defaultdict(list)
        for p in plans: occupancy[p['lane']].append(p)
        valid_plan=all(len(xs)==1 and xs[0]['vin'] in reservations and xs[0]['lane'] in v.get('lanes',{}) and reservations[xs[0]['vin']].get('measured_kg',0)<=v['lanes'][xs[0]['lane']]['weight_kg'] and reservations[xs[0]['vin']].get('length_cm',0)<=v['lanes'][xs[0]['lane']]['length_cm'] and reservations[xs[0]['vin']].get('height_cm',0)<=v['lanes'][xs[0]['lane']]['height_cm'] for xs in occupancy.values()) and bool(plans) and len(plans)==len(confirmed_rows)
        boarded=all(p.get('boarded') for p in plans)
        scanned=all(p.get('boarded') and p.get('unloaded') for p in plans)
        waitlist=[x for x in vehicle_rows if x['vin'] not in stows and x.get('scan_vin')==x['vin'] and x.get('weigh_calibrated')]
        vehicles_ok=bool(confirmed_rows) and all(x.get('scan_vin')==x['vin'] and x.get('weigh_calibrated') and _instant(x['calibration_start'])<=_instant(x['weighed_at'])<_instant(x['calibration_end']) and x.get('hazmat_declared',True) and not (x.get('hazmat_class') and any(other['vin']!=x['vin'] and other.get('hazmat_class') and stows.get(other['vin'],{}).get('lane') in v.get('adjacent_lanes',{}).get(stows.get(x['vin'],{}).get('lane'),[]) for other in confirmed_rows)) for x in confirmed_rows)
        departures=active and tide_ok and weight<=capacity and vehicles_ok and valid_plan and boarded and v.get('captain_signed',False)
        decisions['舱位配载确认']=_decision('确认' if valid_plan else '待改配','一个舱位同一航次只对应一辆合格车',v['id'],*(p['id'] for p in plans))
        decisions['登船与离船闭环']=_decision('闭环' if scanned else '缺口','每辆已配载车应有登船及到港离船扫描',*(p['id'] for p in plans),v['id'])
        decisions['离港放行裁定']=_decision('放行' if departures else '阻断','潮窗、重量、配载、扫描和船长签认共同决定',v['id'],*(p['id'] for p in plans))
        decisions['候补递补资格']=_decision(('可递补' if len(plans)<len(v.get('lanes',{})) else '候补待空位') if waitlist else '无候补','合格而未占舱位的预约按真实空位递补',v['id'],*(x['id'] for x in waitlist))
        for p in plans: states.append(('配载与航行账',p['vin'],'完成' if p.get('boarded') and p.get('unloaded') else '待闭环',p.get('version',1)))
    return decisions,states

RECORD_SPECS={'voyage': ('航次与运力账', 'voyage_id', [('航次版本', ['tide_valid', 'tide_conflict', 'cancelled', 'captain_signed']), ('舱道额度', ['lanes'])]), 'vehicle': ('车辆申报与验车账', 'vin', [('预约单', ['voyage_id', 'vin', 'declared_kg']), ('验车记录', ['scan_vin', 'measured_kg', 'weigh_calibrated', 'hazmat_declared'])]), 'stow': ('配载与航行账', 'vin', [('配载方案', ['voyage_id', 'lane']), ('登离船扫描', ['boarded', 'unloaded'])])}

def business_records(event):
    subsystem,key,specs=RECORD_SPECS[event['kind']]
    return [(subsystem,name,str(event[key]),{field:event.get(field) for field in fields}) for name,fields in specs]
