from collections import defaultdict
from datetime import datetime
from . import tide, vehicle, stow, refund, scale_capture

def _latest(events,kind,key):
    out={}
    for e in events:
        if e['kind']==kind: out[e[key]]=e
    return out

def _decision(result,reason,*ids): return {'result':result,'reason':reason,'evidence':[x for x in ids if x]}

def _instant(value):
    return datetime.fromisoformat(value.replace('Z','+00:00'))

def _confirmed_term(x,book):
    ticket_no=x.get('weigh_ticket')
    if ticket_no:
        ticket=book.ticket(ticket_no)
        if ticket and book.ticket_valid(ticket):
            return scale_capture.WeightTerm.ticket(x['vin'],book.group_key(ticket),ticket['reading_kg'],ticket_no,ticket['revision'],ticket['id'])
        return None
    if x.get('weigh_calibrated',False):
        return scale_capture.WeightTerm.point(x['vin'],x.get('measured_kg',x.get('declared_kg',0)))
    return None

def _boarded_term(x,book):
    ticket_no=x.get('weigh_ticket')
    if ticket_no:
        ticket=book.ticket(ticket_no)
        if ticket and book.ticket_valid(ticket):
            return scale_capture.WeightTerm.ticket(x['vin'],book.group_key(ticket),ticket['reading_kg'],ticket_no,ticket['revision'],ticket['id'])
        return scale_capture.WeightTerm.point(x['vin'],0)
    return scale_capture.WeightTerm.point(x['vin'],x.get('measured_kg',0))

def _weigh_ok(x,book):
    if x.get('weigh_ticket'):
        return vehicle.measured(x,book)
    return bool(x.get('weigh_calibrated')) and _instant(x['calibration_start'])<=_instant(x['weighed_at'])<_instant(x['calibration_end'])

def _lane_over(p,reservations,v,book):
    if p['vin'] not in reservations: return False
    if p['lane'] not in v.get('lanes',{}): return True
    x=reservations[p['vin']]
    if x.get('weigh_ticket'):
        upper=vehicle.weight_upper_kg(x,book)
        return upper is not None and upper>v['lanes'][p['lane']]['weight_kg']
    return x.get('measured_kg',0)>v['lanes'][p['lane']]['weight_kg']

def calculate(events):
    book=scale_capture.ScaleBook.from_events(events)
    voyages=_latest(events,'voyage','voyage_id')
    reservations=_latest(events,'vehicle','vin')
    stows=_latest(events,'stow','vin')
    states=[]; decisions={}; certificates={}
    for voyage_id,v in voyages.items():
        vehicle_rows=[x for x in reservations.values() if x['voyage_id']==voyage_id]
        confirmed_rows=[x for x in vehicle_rows if x.get('status','confirmed')=='confirmed']
        plans=[x for x in stows.values() if x['voyage_id']==voyage_id]
        tide_ok,tide_conflict=tide.evaluate(v)
        capacity=sum(l['weight_kg'] for l in v.get('lanes',{}).values())
        scale_involved=any(x.get('weigh_ticket') for x in vehicle_rows)
        lane_over=any(_lane_over(p,reservations,v,book) for p in plans)
        if scale_involved:
            confirmed_terms=[t for t in (_confirmed_term(x,book) for x in confirmed_rows) if t]
            boarded_weighted=[(_boarded_term(reservations[p['vin']],book),v['lanes'][p['lane']].get('lever_arm_cm',0)) for p in plans if p.get('boarded') and p['vin'] in reservations and p['lane'] in v.get('lanes',{})]
            cert=scale_capture.voyage_certificate(book,v,confirmed_terms,boarded_weighted,capacity)
            certificates[voyage_id]=cert
            weight_lo=cert['confirmed_weight_kg']['lower']; weight_hi=cert['confirmed_weight_kg']['upper']
            stab=cert['center_of_gravity_cm']
            quota_status=cert['quota']['status']
            quota_result='分区超载' if lane_over or quota_status=='exceeded' else ('有余量' if quota_status=='within' else '边界待定')
            quota_reason=f"全船重量区间[{weight_lo:.0f},{weight_hi:.0f}]千克对容量{capacity}千克，共享偏差见证书"
            stab_result={'within':'平衡','outside':'失衡或无实载','empty':'失衡或无实载'}.get(stab['status'],'边界待定')
            if stab['status']=='empty':
                stab_reason='无有效实载磅票，重心不可证'
            else:
                stab_reason=f"实载总重[{cert['boarded_weight_kg']['lower']:.0f},{cert['boarded_weight_kg']['upper']:.0f}]千克，横向重心区间[{stab['lower']:.2f},{stab['upper']:.2f}]厘米，限值[{stab['limits'][0]},{stab['limits'][1]}]厘米"
            stability_ok=stab['status']=='within'
            weight_ok=weight_hi<=capacity
            group_desc='；'.join(f"{g['key']}偏差[{g['bias_kg'][0]},{g['bias_kg'][1]}]千克({len(g['tickets'])}票)" for g in cert['bias_groups']) or '无有效磅票组'
            decisions[f'共享偏差证书:{voyage_id}']=_decision('已签发' if cert['bias_groups'] else '无有效磅票',f'共享零点偏差分组：{group_desc}',v['id'],*(g['calibration_event'] for g in cert['bias_groups']),*(t['event'] for g in cert['bias_groups'] for t in g['tickets']))
        else:
            weight=sum(x.get('measured_kg',x.get('declared_kg',0)) for x in confirmed_rows if x.get('weigh_calibrated',False))
            boarded_weight=sum(reservations[p['vin']].get('measured_kg',0) for p in plans if p.get('boarded') and p['vin'] in reservations)
            moment=sum(reservations[p['vin']].get('measured_kg',0)*v['lanes'][p['lane']].get('lever_arm_cm',0) for p in plans if p.get('boarded') and p['vin'] in reservations and p['lane'] in v['lanes'])
            center=moment/boarded_weight if boarded_weight else 0
            stability_ok=boarded_weight>0 and v.get('stability_min_cm',-30)<=center<=v.get('stability_max_cm',30)
            quota_result='有余量' if weight<=capacity and not lane_over else '分区超载'
            quota_reason='有效实测总重与各舱道额定重量比较'
            stab_result='平衡' if stability_ok else '失衡或无实载'
            stab_reason=f'实载总重 {boarded_weight} 千克，横向重心 {center:.1f} 厘米'
            weight_ok=weight<=capacity
        active=not v.get('cancelled',False)
        decisions.update({
            '航次潮窗核定':_decision('开放' if tide_ok and active else '停航','潮窗有效、无冲突且未撤销' if tide_ok and active else '潮窗冲突/失效或航次停航',v['id']),
            '舱道分区额度':_decision(quota_result,quota_reason,v['id'],*(x['id'] for x in vehicle_rows)),
            '稳性裁定':_decision(stab_result,stab_reason,v['id'],*(p['id'] for p in plans)),
            '潮窗冲突冻结':_decision('冻结' if tide_conflict else '正常','冲突潮窗不得继续开放',v['id']),
            '停航退票责任':_decision('应退票' if refund.due(active,vehicle_rows,stows) else '无退票','只对停航且未实际登船的预约生成责任',v['id'])})
        states.append(('航次与运力账',voyage_id,'开放' if tide_ok and active else '停航',v.get('version',1)))
        for x in vehicle_rows:
            vin=x['vin']; matched=vehicle.matched(x)
            measured=vehicle.measured(x,book)
            lane=stows.get(vin,{}).get('lane')
            neighbors=v.get('adjacent_lanes',{}).get(lane,[])
            haz_neighbors=[other for other in vehicle_rows if other['vin']!=vin and other.get('hazmat_class') and stows.get(other['vin'],{}).get('lane') in neighbors]
            haz=x.get('hazmat_declared',True) and not (x.get('hazmat_class') and haz_neighbors)
            decisions[f'车辆身份复核:{vin}']=_decision('通过' if matched else '拒载','预约 VIN 必须与闸口扫描一致',x['id'])
            ticket_no=x.get('weigh_ticket')
            if ticket_no:
                ticket=book.ticket(ticket_no)
                if ticket and book.ticket_valid(ticket):
                    key=book.group_key(ticket); blo,bhi=book.bias_interval(key)
                    measured_reason=f'磅票{ticket_no}属共享偏差组{key[0]}|{key[1]}，真重=读数-组偏差，偏差区间[{blo},{bhi}]千克'
                    trust_reason=f'磅票{ticket_no}绑定校准{key[1]}版本{book.calibration_of(key).get("version",1)}，见共享偏差证书'
                else:
                    measured_reason=f'磅票{ticket_no}缺失、撤销或校准时段不覆盖称重时刻'
                    trust_reason=f'磅票{ticket_no}未通过证书校验'
                decisions[f'实测重量裁定:{vin}']=_decision('有效' if measured else '待复称',measured_reason,x['id'],ticket['id'] if ticket else None)
                decisions[f'称重校准可信度:{vin}']=_decision('可信' if measured else '失效',trust_reason,x['id'],ticket['id'] if ticket else None)
            else:
                decisions[f'实测重量裁定:{vin}']=_decision('有效' if measured else '待复称','校准有效的实测重量参与全船额度',x['id'])
                decisions[f'称重校准可信度:{vin}']=_decision('可信' if x.get('weigh_calibrated') else '失效','校准失效的重量不应被签认',x['id'])
            decisions[f'危险品隔离核验:{vin}']=_decision('合格' if haz else '拒载','危险品须如实申报并隔离',x['id'])
            states.append(('车辆申报与验车账',vin,'合格' if matched and measured and haz else '拒载或待核',x.get('version',1)))
        valid_plan=stow.valid_plan(plans,reservations,v,confirmed_rows)
        hazard_conflict=any(x.get('hazmat_class') and any(other['vin']!=x['vin'] and other.get('hazmat_class') and stows.get(other['vin'],{}).get('lane') in v.get('adjacent_lanes',{}).get(stows.get(x['vin'],{}).get('lane'),[]) for other in confirmed_rows) for x in confirmed_rows)
        confirmed_ready=all(vehicle.matched(x) and vehicle.measured(x,book) and x.get('hazmat_declared',True) for x in confirmed_rows)
        plan_eligible=valid_plan and tide_ok and active and not lane_over and not hazard_conflict and confirmed_ready
        boarded=all(p.get('boarded') for p in plans)
        scanned=stow.scanned(plans)
        waitlist=sorted((x for x in vehicle_rows if x['vin'] not in stows and x.get('scan_vin')==x['vin'] and _weigh_ok(x,book) and x.get('hazmat_declared',True)),
                        key=lambda x:(-x.get('priority',0),x['id']))
        occupied_lanes={p['lane'] for p in plans if p.get('boarded')}
        free_lanes=set(v.get('lanes',{}))-occupied_lanes
        def candidate_fits(x,lane_id):
            lane=v['lanes'][lane_id]
            if x.get('weigh_ticket'):
                upper=vehicle.weight_upper_kg(x,book)
                size_ok=upper is not None and upper<=lane['weight_kg'] and x.get('length_cm',0)<=lane['length_cm'] and x.get('height_cm',0)<=lane['height_cm']
            else:
                size_ok=x.get('measured_kg',0)<=lane['weight_kg'] and x.get('length_cm',0)<=lane['length_cm'] and x.get('height_cm',0)<=lane['height_cm']
            neighbors=v.get('adjacent_lanes',{}).get(lane_id,[])
            hazard_ok=not x.get('hazmat_class') or not any(reservations[p['vin']].get('hazmat_class') and p['lane'] in neighbors for p in plans if p.get('boarded') and p['vin'] in reservations)
            return size_ok and hazard_ok
        eligible_candidate=next((x for x in waitlist if any(candidate_fits(x,lane) for lane in free_lanes)),None)
        vehicles_ok=bool(confirmed_rows) and all(vehicle.matched(x) and vehicle.measured(x,book) and x.get('hazmat_declared',True) and not (x.get('hazmat_class') and any(other['vin']!=x['vin'] and other.get('hazmat_class') and stows.get(other['vin'],{}).get('lane') in v.get('adjacent_lanes',{}).get(stows.get(x['vin'],{}).get('lane'),[]) for other in confirmed_rows)) for x in confirmed_rows)
        departures=active and tide_ok and weight_ok and stability_ok and vehicles_ok and valid_plan and boarded and v.get('captain_signed',False)
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
        measured=vehicle.measured(x,book)
        decisions[f'车辆身份复核:{vin}']=_decision('通过' if matched else '拒载','预约 VIN 与闸口扫描独立核验；航次证据尚未导入',x['id'])
        decisions[f'实测重量裁定:{vin}']=_decision('有效' if measured else '待复称','独立校准凭据覆盖称重时刻',x['id'])
        decisions[f'危险品隔离核验:{vin}']=_decision('待配载' if x.get('hazmat_class') else '合格','危险品邻接要在实际配载后才能核定',x['id'])
        decisions[f'称重校准可信度:{vin}']=_decision('可信' if measured else '失效','称重凭据单独留存',x['id'])
        states.append(('车辆申报与验车账',vin,'合格' if matched and measured else '待核',x.get('version',1)))
    for vin,p in stows.items():
        if p['voyage_id'] in voyages: continue
        decisions[f'登船与离船闭环:{vin}']=_decision('闭环' if p.get('boarded') and p.get('unloaded') else '缺口','登离船扫描可独立登记，最终放行待航次证据',p['id'])
        states.append(('配载与航行账',vin,'待航次核定',p.get('version',1)))
    return decisions,states,certificates

RECORD_SPECS={'voyage': ('航次与运力账', 'voyage_id', [('航次版本', ['tide_valid', 'tide_conflict', 'cancelled', 'captain_signed']), ('舱道额度', ['lanes'])]), 'vehicle': ('车辆申报与验车账', 'vin', [('预约单', ['voyage_id', 'vin', 'declared_kg']), ('验车记录', ['scan_vin', 'measured_kg', 'weigh_calibrated', 'hazmat_declared'])]), 'stow': ('配载与航行账', 'vin', [('配载方案', ['voyage_id', 'lane']), ('登离船扫描', ['boarded', 'unloaded'])])}

SCALE_RECORD_SPECS={'calibration': ('校准记录', 'id', ['scale_id', 'calibration_id', 'valid_from', 'valid_to', 'zero_bias_kg', 'points']), 'ticket': ('磅票', 'ticket_no', ['vin', 'voyage_id', 'scale_id', 'calibration_id', 'reading_kg', 'weighed_at', 'revision', 'void', 'ocr'])}

def business_records(event):
    if event['kind']=='scale':
        entity_type,key,fields=SCALE_RECORD_SPECS[event.get('role','ticket')]
        return [('磅票与校准账',entity_type,str(event.get(key,event['id'])),{field:event.get(field) for field in fields})]
    subsystem,key,specs=RECORD_SPECS[event['kind']]
    return [(subsystem,name,str(event[key]),{field:event.get(field) for field in fields}) for name,fields in specs]
