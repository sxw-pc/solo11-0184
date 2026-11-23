import json
from pathlib import Path

import pytest

from ferryload import custody, seal, seal_capture

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'


def load_records(name='seal-transfer.json'):
    return seal_capture.load(FIXTURES / name)


def test_legal_midroute_reseal_completes_handover():
    records = load_records()
    lin = seal.lineage(records, 'C1')
    assert lin['chain'] == ['S1', 'S2']
    first, second = lin['segments']
    assert first['seal'] == 'S1'
    assert (first['from']['port'], first['to']['port']) == ('A', 'B')
    assert first['to']['event'] == 'I7'
    assert second['seal'] == 'S2'
    assert (second['from']['port'], second['to']['port']) == ('B', 'C')
    assert second['signed'] and lin['signed']
    assert lin['first_break'] is None and lin['conflicts'] == []
    assert lin['arrival_port'] == 'C'
    result = custody.assess(lin)
    assert result['result'] == '合法换封后完整交接'
    # 只看出发/到达字符串会误判异常：S1 != S2，但换封有连续查验证据
    assert not seal.unchanged({'origin_seal': 'S1', 'arrival_seal': 'S2'})


def test_cross_container_issuance_conflict_is_pending():
    records = load_records()
    lin = seal.lineage(records, 'C2')
    kinds = [c['kind'] for c in lin['conflicts']]
    assert '发行流水重复' in kinds
    n9 = next(c for c in lin['conflicts'] if c['kind'] == '发行流水重复')
    assert n9['issuance'] == 'N9' and n9['first'] == 'R3' and n9['first_cargo'] == 'C1'
    assert '封签实体被别箱占用' in kinds
    assert lin['first_break']['record'] == 'R6'
    assert seal.reused_issuances(records) == ['N9']
    assert custody.assess(lin)['result'] == '无法归属待核'
    # 不会给出两份自相矛盾的完整交接：只有 C1 完整，C2 待核
    results = custody.assess_all(records)
    assert results['C1']['result'] == '合法换封后完整交接'
    assert results['C2']['result'] == '无法归属待核'


def test_occurrence_time_beats_upload_order():
    records = {r['record_id']: r for r in load_records()}
    r3, r6 = records['R3'], records['R6']
    assert r3['occurred_at'] < r6['occurred_at']  # C1 的 N9 发行在先
    assert r6['recorded_at'] < r3['recorded_at']  # 但模糊登记上传更早
    lins = seal.lineages(list(records.values()))
    assert lins['C1']['conflicts'] == []  # N9 按发生时刻归 C1，而非先上传的 C2
    assert lins['C2']['first_break']['record'] == 'R6'


def test_sealed_transfer_keeps_original_seal_to_other_port():
    records = load_records()
    departure = next(r for r in records if r['record_id'] == 'R1')
    rerouted = seal_capture.capture([
        departure,
        {'record_id': 'T1', 'kind': 'transfer', 'cargo_id': 'C1', 'vin': 'VIN-C1',
         'port': 'B', 'event': 'TRN-C1', 'old_seal': 'S1',
         'occurred_at': '2026-11-20T10:00:00Z', 'recorded_at': '2026-11-20T10:01:00Z'},
        {'record_id': 'T2', 'kind': 'arrival', 'cargo_id': 'C1', 'vin': 'VIN-C1',
         'port': 'D', 'event': 'ARR-C1-D', 'old_seal': 'S1',
         'occurred_at': '2026-11-20T12:00:00Z', 'recorded_at': '2026-11-20T12:05:00Z'},
    ])
    transfer_lin = seal.lineage(rerouted, 'C1')
    assert transfer_lin['chain'] == ['S1']
    assert len(transfer_lin['segments']) == 1
    segment = transfer_lin['segments'][0]
    assert [c['port'] for c in segment['checkpoints']] == ['B']
    transfer = custody.assess(transfer_lin)
    reseal = custody.assess(seal.lineage(records, 'C1'))
    assert transfer['result'] == '原封交接'
    assert transfer['arrival_port'] == 'D'  # C 港没有到达签收
    assert reseal['result'] == '合法换封后完整交接'
    assert reseal['arrival_port'] == 'C'
    # 同一出发状态的两条真实路线，谱系、签收口岸不同，不可混为一谈
    assert transfer['result'] != reseal['result']
    assert transfer['chain'] != reseal['chain']


def test_late_photo_and_revocation_close_c2():
    records = load_records()
    revocation = {'record_id': 'R7', 'kind': 'revocation', 'revokes': 'R6'}
    late_photo = {'record_id': 'R8', 'kind': 'arrival', 'cargo_id': 'C2',
                  'vin': 'VIN-C2', 'port': 'C', 'event': 'ARR-C2',
                  'old_seal': 'S3', 'source': 'photo',
                  'occurred_at': '2026-11-20T11:05:00Z',
                  'recorded_at': '2026-11-20T12:00:00Z'}
    # 只有迟到照片、没有撤销单：模糊登记的冲突仍在，不能凭最后字符串闭环
    photo_only = seal_capture.capture(list(records) + [late_photo])
    assert custody.assess(seal.lineage(photo_only, 'C2'))['result'] == '无法归属待核'
    # 撤销单勘误模糊登记后，迟到照片证明 C2 实际封签仍是 S3，原封闭环
    healed = seal_capture.capture(list(records) + [revocation, late_photo])
    assert seal.reused_issuances(healed) == []
    lin = seal.lineage(healed, 'C2')
    assert lin['chain'] == ['S3'] and lin['signed'] and lin['conflicts'] == []
    assert custody.assess(lin)['result'] == '原封交接'
    assert custody.assess(seal.lineage(healed, 'C1'))['result'] == '合法换封后完整交接'


def test_missing_inspection_order_shows_breakpoint_first():
    records = seal_capture.capture([
        {'record_id': 'M1', 'kind': 'departure', 'cargo_id': 'C5', 'vin': 'VIN-C5',
         'port': 'A', 'event': 'DEP-C5', 'new_seal': 'S5',
         'occurred_at': '2026-11-20T08:00:00Z'},
        {'record_id': 'M2', 'kind': 'unseal', 'cargo_id': 'C5', 'vin': 'VIN-C5',
         'port': 'B', 'event': None, 'old_seal': 'S5', 'reason': '海关查验',
         'occurred_at': '2026-11-20T10:00:00Z'},
        {'record_id': 'M3', 'kind': 'reseal', 'cargo_id': 'C5', 'vin': 'VIN-C5',
         'port': 'B', 'event': 'I8', 'issuance': 'N5', 'new_seal': 'S6',
         'occurred_at': '2026-11-20T10:05:00Z'},
        {'record_id': 'M4', 'kind': 'arrival', 'cargo_id': 'C5', 'vin': 'VIN-C5',
         'port': 'C', 'event': 'ARR-C5', 'old_seal': 'S6',
         'occurred_at': '2026-11-20T11:00:00Z'},
    ])
    lin = seal.lineage(records, 'C5')
    assert lin['first_break']['record'] == 'M2'
    assert '查验单' in lin['first_break']['reason']
    result = custody.assess(lin)
    # 缺查验单必须先显示断点，不能按首尾字符串自动选择路线
    assert result['result'] == '未交接断链'
    assert result['first_break']['record'] == 'M2'


def test_legacy_single_segment_fixture_still_consistent():
    record = json.loads((FIXTURES / 'cargo-seal-ok.json').read_text(encoding='utf-8'))
    assert seal.unchanged(record)
    assert custody.assess(record) == {'cargo_id': 'CARGO-1', 'result': '封签一致'}


def test_capture_preserves_registry_fields():
    records = {r['record_id']: r for r in load_records()}
    r2 = records['R2']
    assert r2['cargo_id'] == 'C1' and r2['vin'] == 'VIN-C1'
    assert r2['port'] == 'B' and r2['event'] == 'I7'
    assert r2['old_seal'] == 'S1' and r2['reason'] == '海关查验'
    assert r2['occurred_at'] < r2['recorded_at'] and r2['page'] == 13
    r3 = records['R3']
    assert r3['issuance'] == 'N9' and r3['new_seal'] == 'S2'
    assert r3['source'] == 'photo'
    assert all('revokes' in r for r in records.values())


def test_capture_integrity():
    with pytest.raises(ValueError):  # 记录号重复
        seal_capture.capture([
            {'record_id': 'D1', 'kind': 'departure', 'cargo_id': 'C1', 'port': 'A',
             'new_seal': 'S1', 'occurred_at': '2026-11-20T08:00:00Z'},
            {'record_id': 'D1', 'kind': 'arrival', 'cargo_id': 'C1', 'port': 'C',
             'old_seal': 'S1', 'occurred_at': '2026-11-20T11:00:00Z'},
        ])
    with pytest.raises(ValueError):  # 撤销指向未知记录
        seal_capture.capture([{'record_id': 'X1', 'kind': 'revocation', 'revokes': 'NOPE'}])
    with pytest.raises(ValueError):  # 施封缺发行流水
        seal_capture.capture([
            {'record_id': 'X2', 'kind': 'reseal', 'cargo_id': 'C1', 'port': 'B',
             'new_seal': 'S2', 'occurred_at': '2026-11-20T10:05:00Z'}])
    rec = seal_capture.capture([
        {'record_id': 'X3', 'kind': 'departure', 'cargo_id': 'C1', 'port': 'A',
         'new_seal': 'S1', 'occurred_at': '2026-11-20T08:00:00Z'}])[0]
    assert rec['recorded_at'] == '2026-11-20T08:00:00Z'  # 录入时间缺省回填发生时间
