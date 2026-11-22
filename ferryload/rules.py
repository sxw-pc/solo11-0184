from collections import defaultdict
from datetime import datetime
from . import tide, vehicle, stow, refund

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
        tide_ok,tide_conflict=tide.evaluate(v)
        capacity=sum(l['weight_kg'] for l in v.get('lanes',{}).values())
        weight=sum(x.get('measured_kg',x.get('declared_kg',0)) for x in confirmed_rows if x.get('weigh_calibrated',False))
        boarded_weight=sum(reservations[p['vin']].get('measured_kg',0) for p in plans if p.get('boarded') and p['vin'] in reservations)
        moment=sum(reservations[p['vin']].get('measured_kg',0)*v['lanes'][p['lane']].get('lever_arm_cm',0) for p in plans if p.get('boarded') and p['vin'] in reservations and p['lane'] in v['lanes'])
        center=moment/boarded_weight if boarded_weight else 0
        stability_ok=boarded_weight>0 and v.get('stability_min_cm',-30)<=center<=v.get('stability_max_cm',30)
        lane_over=any(p['lane'] not in v.get('lanes',{}) or reservations[p['vin']].get('measured_kg',0)>v['lanes'][p['lane']]['weight_kg'] for p in plans if p['vin'] in reservations)
        active=not v.get('cancelled',False)
        decisions.update({
            '航次潮窗核定':_decision('开放' if tide_ok and active else '停航','潮窗有效、无冲突且未撤销' if tide_ok and active else '潮窗冲突/失效或航次停航',v['id']),
            '舱道分区额度':_decision('有余量' if weight<=capacity and not lane_over else '分区超载','有效实测总重与各舱道额定重量比较',v['id'],*(x['id'] for x in vehicle_rows)),
            '稳性裁定':_decision('平衡' if stability_ok else '失衡或无实载',f'实载总重 {boarded_weight} 千克，横向重心 {center:.1f} 厘米',v['id'],*(p['id'] for p in plans)),
            '潮窗冲突冻结':_decision('冻结' if tide_conflict else '正常','冲突潮窗不得继续开放',v['id']),
            '停航退票责任':_decision('应退票' if refund.due(active,vehicle_rows,stows) else '无退票','只对停航且未实际登船的预约生成责任',v['id'])})
        states.append(('航次与运力账',voyage_id,'开放' if tide_ok and active else '停航',v.get('version',1)))
        for x in vehicle_rows:
            vin=x['vin']; matched=vehicle.matched(x)
            measured=vehicle.measured(x)
            lane=stows.get(vin,{}).get('lane')
            neighbors=v.get('adjacent_lanes',{}).get(lane,[])
            haz_neighbors=[other for other in vehicle_rows if other['vin']!=vin and other.get('hazmat_class') and stows.get(other['vin'],{}).get('lane') in neighbors]
            haz=x.get('hazmat_declared',True) and not (x.get('hazmat_class') and haz_neighbors)
            decisions[f'车辆身份复核:{vin}']=_decision('通过' if matched else '拒载','预约 VIN 必须与闸口扫描一致',x['id'])
            decisions[f'实测重量裁定:{vin}']=_decision('有效' if measured else '待复称','校准有效的实测重量参与全船额度',x['id'])
            decisions[f'危险品隔离核验:{vin}']=_decision('合格' if haz else '拒载','危险品须如实申报并隔离',x['id'])
            decisions[f'称重校准可信度:{vin}']=_decision('可信' if x.get('weigh_calibrated') else '失效','校准失效的重量不应被签认',x['id'])
            states.append(('车辆申报与验车账',vin,'合格' if matched and measured and haz else '拒载或待核',x.get('version',1)))
        valid_plan=stow.valid_plan(plans,reservations,v,confirmed_rows)
        hazard_conflict=any(x.get('hazmat_class') and any(other['vin']!=x['vin'] and other.get('hazmat_class') and stows.get(other['vin'],{}).get('lane') in v.get('adjacent_lanes',{}).get(stows.get(x['vin'],{}).get('lane'),[]) for other in confirmed_rows) for x in confirmed_rows)
        confirmed_ready=all(vehicle.matched(x) and vehicle.measured(x) and x.get('hazmat_declared',True) for x in confirmed_rows)
        plan_eligible=valid_plan and tide_ok and active and not lane_over and not hazard_conflict and confirmed_ready
        boarded=all(p.get('boarded') for p in plans)
        scanned=stow.scanned(plans)
        waitlist=sorted((x for x in vehicle_rows if x['vin'] not in stows and x.get('scan_vin')==x['vin'] and x.get('weigh_calibrated') and _instant(x['calibration_start'])<=_instant(x['weighed_at'])<_instant(x['calibration_end']) and x.get('hazmat_declared',True)),
                        key=lambda x:(-x.get('priority',0),x['id']))
        occupied_lanes={p['lane'] for p in plans if p.get('boarded')}
        free_lanes=set(v.get('lanes',{}))-occupied_lanes
        def candidate_fits(x,lane_id):
            lane=v['lanes'][lane_id]
            size_ok=x.get('measured_kg',0)<=lane['weight_kg'] and x.get('length_cm',0)<=lane['length_cm'] and x.get('height_cm',0)<=lane['height_cm']
            neighbors=v.get('adjacent_lanes',{}).get(lane_id,[])
            hazard_ok=not x.get('hazmat_class') or not any(reservations[p['vin']].get('hazmat_class') and p['lane'] in neighbors for p in plans if p.get('boarded') and p['vin'] in reservations)
            return size_ok and hazard_ok
        eligible_candidate=next((x for x in waitlist if any(candidate_fits(x,lane) for lane in free_lanes)),None)
        vehicles_ok=bool(confirmed_rows) and all(vehicle.matched(x) and vehicle.measured(x) and x.get('hazmat_declared',True) and not (x.get('hazmat_class') and any(other['vin']!=x['vin'] and other.get('hazmat_class') and stows.get(other['vin'],{}).get('lane') in v.get('adjacent_lanes',{}).get(stows.get(x['vin'],{}).get('lane'),[]) for other in confirmed_rows)) for x in confirmed_rows)
        departures=active and tide_ok and weight<=capacity and stability_ok and vehicles_ok and valid_plan and boarded and v.get('captain_signed',False)
        decisions['舱位配载确认']=_decision('确认' if plan_eligible else '待改配','舱位唯一且航次潮窗、额度和状态仍有效',v['id'],*(p['id'] for p in plans))
        decisions['登船与离船闭环']=_decision('闭环' if scanned else '缺口','每辆已配载车应有登船及到港离船扫描',*(p['id'] for p in plans),v['id'])
        decisions['离港放行裁定']=_decision('放行' if departures else '阻断','潮窗、重量、配载、扫描和船长签认共同决定',v['id'],*(p['id'] for p in plans))
        wait_result=('可递补:'+eligible_candidate['vin']) if eligible_candidate else (('候补待空位:'+waitlist[0]['vin']) if waitlist and not free_lanes else ('无合格候补' if waitlist else '无候补'))
        decisions['候补递补资格']=_decision(wait_result,'合格而未占舱位的预约按优先级、舱道尺寸和危险品隔离递补',v['id'],*(x['id'] for x in waitlist))
        decisions['实载额度释放']=_decision(f'{len(free_lanes)}条空舱道','只将已经登船的车辆计入实载占用；缺扫描的原计划释放可用额度',v['id'],*(p['id'] for p in plans))
        for p in plans: states.append(('配载与航行账',p['vin'],('完成' if p.get('boarded') and p.get('unloaded') else '待闭环') if plan_eligible else '待改配',p.get('version',1)))
    for vin,x in reservations.items():
        if x['voyage_id'] in voyages: continue
        matched=vehicle.matched(x)
        measured=vehicle.measured(x)
        decisions[f'车辆身份复核:{vin}']=_decision('通过' if matched else '拒载','预约 VIN 与闸口扫描独立核验；航次证据尚未导入',x['id'])
        decisions[f'实测重量裁定:{vin}']=_decision('有效' if measured else '待复称','独立校准凭据覆盖称重时刻',x['id'])
        decisions[f'危险品隔离核验:{vin}']=_decision('待配载' if x.get('hazmat_class') else '合格','危险品邻接要在实际配载后才能核定',x['id'])
        decisions[f'称重校准可信度:{vin}']=_decision('可信' if measured else '失效','称重凭据单独留存',x['id'])
        states.append(('车辆申报与验车账',vin,'合格' if matched and measured else '待核',x.get('version',1)))
    for vin,p in stows.items():
        if p['voyage_id'] in voyages: continue
        decisions[f'登船与离船闭环:{vin}']=_decision('闭环' if p.get('boarded') and p.get('unloaded') else '缺口','登离船扫描可独立登记，最终放行待航次证据',p['id'])
        states.append(('配载与航行账',vin,'待航次核定',p.get('version',1)))
    return decisions,states

RECORD_SPECS={'voyage': ('航次与运力账', 'voyage_id', [('航次版本', ['tide_valid', 'tide_conflict', 'cancelled', 'captain_signed']), ('舱道额度', ['lanes'])]), 'vehicle': ('车辆申报与验车账', 'vin', [('预约单', ['voyage_id', 'vin', 'declared_kg']), ('验车记录', ['scan_vin', 'measured_kg', 'weigh_calibrated', 'hazmat_declared'])]), 'stow': ('配载与航行账', 'vin', [('配载方案', ['voyage_id', 'lane']), ('登离船扫描', ['boarded', 'unloaded'])])}

def business_records(event):
    subsystem,key,specs=RECORD_SPECS[event['kind']]
    return [(subsystem,name,str(event[key]),{field:event.get(field) for field in fields}) for name,fields in specs]

