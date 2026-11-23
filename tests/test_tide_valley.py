"""浅水潮谷连续包络裁定测试。

验收主线：
1. 副本原 220 cm 放行与窗口过期阻断（evaluate 旧契约不变）；
2. 双端 220、中途 170 的两世界在同一原件下都物理可行；
3. 连续包络（而非端点线性插值/密采样）决定“冻结待补测”；
4. 补测与卸车两条合法恢复链对配载、稳性、候补及离港裁决的不同影响；
5. 水尺、曲线见证、车辆装载与签名版本可审计，历史放行证书不被覆盖。
"""

import json
import tempfile
import unittest
from pathlib import Path

from ferryload.tide import assess_channel, curve_feasible, evaluate, hull_draft_cm
from ferryload.store import audit, certificate_history, connect, import_file, report

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'


def load(name):
    return json.loads((FIX / name).read_text(encoding='utf-8'))


class SeedContractTests(unittest.TestCase):
    def test_original_220_window_releases_and_expired_window_blocks(self):
        voyage = load('voyage-ok.json')[0]
        # 10:00 离港、08:00–12:00 记录 220 cm：旧契约仍为 (True, False)
        self.assertEqual(evaluate(voyage), (True, False))
        # 仅把窗口截止改成 09:00，车辆与签名不动：同一 calculate 链路被阻断
        expired = dict(voyage)
        expired['tide_windows'] = [{'start': '2026-06-01T08:00:00Z',
                                    'end': '2026-06-01T09:00:00Z', 'level_cm': 220}]
        self.assertEqual(evaluate(expired), (False, False))


class TwoWorldEnvelopeTests(unittest.TestCase):
    def setUp(self):
        valley = load('tide-valley.json')
        self.by_id = {e['id']: e for e in valley}
        self.channel = self.by_id['CH1']
        self.end_gauges = [self.by_id['G1'], self.by_id['G2']]
        self.voyage = load('voyage-ok.json')[0]
        self.boarding_v1 = [
            {'vin': 'VIN1', 'weight_kg': 1100, 'lane': 'L1', 'lever_arm_cm': -100, 'load_version': 1},
            {'vin': 'VIN2', 'weight_kg': 850, 'lane': 'L2', 'lever_arm_cm': 100, 'load_version': 1}]
        self.world_a = [
            {'at': '2026-06-01T10:00:00Z', 'level_cm': 220},
            {'at': '2026-06-01T11:00:00Z', 'level_cm': 220},
            {'at': '2026-06-01T12:00:00Z', 'level_cm': 220}]
        self.world_b = [
            {'at': '2026-06-01T10:00:00Z', 'level_cm': 220},
            {'at': '2026-06-01T11:00:00Z', 'level_cm': 170},
            {'at': '2026-06-01T12:00:00Z', 'level_cm': 220}]

    def test_draft_tracks_actual_loaded_weight(self):
        # 已登船 1950 kg → 吃水 160，需求 160+20 净深 = 180
        self.assertAlmostEqual(hull_draft_cm(self.voyage, 1950), 160.0, places=1)
        # 卸下 1100 kg 重车，只剩 850 kg → 吃水 140，需求 160
        self.assertAlmostEqual(hull_draft_cm(self.voyage, 850), 140.0, places=1)

    def test_both_worlds_are_physically_feasible_under_same_originals(self):
        # 两世界给现有代码的 voyage/水尺/航速/车辆/签认完全相同，
        # 但两条真实曲线都与授权包络和两端水尺一致
        self.assertTrue(curve_feasible(self.channel, self.end_gauges, self.world_a))
        self.assertTrue(curve_feasible(self.channel, self.end_gauges, self.world_b))
        # 世界乙在 11:00 差 10 cm 而搁浅（220 端点并不能排除 170 的谷值）
        self.assertEqual(self.world_b[1]['level_cm'] - 180, -10)

    def test_continuous_envelope_freezes_instead_of_picking_pretty_interpolation(self):
        a = assess_channel(self.channel, self.end_gauges, self.voyage, self.boarding_v1)
        # 同一原件先输出最低 170、最高 220 与“不可无条件放行”
        self.assertEqual(a['verdict'], 'frozen')
        self.assertEqual(a['overall_min_feasible_cm'], 170.0)
        self.assertEqual(a['overall_max_feasible_cm'], 220.0)
        # 最危险点在浅滩附近，最坏缺 10 cm
        self.assertEqual(a['most_dangerous']['required_cm'], 180.0)
        self.assertEqual(a['most_dangerous']['min_margin_cm'], -10.0)
        self.assertLess(a['most_dangerous']['min_feasible_cm'], 180.0)
        # 曲线见证本身必须尊重包络带、速率锥与水尺 pin
        self.assertTrue(a['witnesses_honor_envelope'])
        # 见证下界在 11:00 可取到 170（世界乙），上界为 220（世界甲）
        at_11 = next(p for p in a['point_evidence'] if p['at'].startswith('2026-06-01T11:00:00'))
        self.assertEqual((at_11['min_feasible_cm'], at_11['max_feasible_cm']), (170.0, 220.0))

    def test_dense_sampling_linear_interpolation_still_wrongly_passes_world_b(self):
        # 反例固定：即使在已采样点验深并用端点线性插值，11:00 也会被算成 220，
        # 从而在世界乙误放行——证明密采样/插值不能替代连续包络。
        endpoint_levels = sorted(float(g['level_cm']) for g in self.end_gauges)
        interpolated_midpoint = endpoint_levels[0] + (endpoint_levels[1] - endpoint_levels[0]) * 0.5
        self.assertEqual(interpolated_midpoint, 220)
        # 而连续包络给出的是区间 170–220，故系统必须冻结
        a = assess_channel(self.channel, self.end_gauges, self.voyage, self.boarding_v1)
        self.assertEqual(a['verdict'], 'frozen')
        at_11 = next(p for p in a['point_evidence'] if p['at'].startswith('2026-06-01T11:00:00'))
        self.assertEqual((at_11['min_feasible_cm'], at_11['max_feasible_cm']), (170.0, 220.0))

    def test_rate_cone_and_envelope_reject_curve_outside_band(self):
        # 一条超出包络上界的曲线不被承认为可行
        rogue = [{'at': '2026-06-01T10:00:00Z', 'level_cm': 220},
                 {'at': '2026-06-01T11:00:00Z', 'level_cm': 230},
                 {'at': '2026-06-01T12:00:00Z', 'level_cm': 220}]
        self.assertFalse(curve_feasible(self.channel, self.end_gauges, rogue))
        # 与水尺原件矛盾的曲线也被拒绝
        contradict = [{'at': '2026-06-01T10:00:00Z', 'level_cm': 200},
                      {'at': '2026-06-01T12:00:00Z', 'level_cm': 220}]
        self.assertFalse(curve_feasible(self.channel, self.end_gauges, contradict))

    def test_calibration_expired_gauge_is_rejected(self):
        bad = dict(self.by_id['G1'], gauge_id='GAUGE-BAD', calibrated=False)
        a = assess_channel(self.channel, [bad, self.by_id['G2']], self.voyage, self.boarding_v1)
        self.assertIn('GAUGE-BAD', a['gauges_rejected'])
        self.assertEqual(a['verdict'], 'frozen')


class RecoveryChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = FIX
        valley = load('tide-valley.json')
        self.by_id = {e['id']: e for e in valley}

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, payload):
        path = Path(self.tmp.name) / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        return path

    def _base(self, db):
        import_file(db, 'voyage', self._write('voyage.json', load('voyage-ok.json')))
        import_file(db, 'vehicle', self._write('vehicle.json', load('vehicle-ok.json')))
        import_file(db, 'stow', self._write('stow.json', load('stow-ok.json')))
        import_file(db, 'channel', self._write('channel.json', [self.by_id['CH1']]))
        import_file(db, 'gauge', self._write('gauge-end.json', [self.by_id['G1'], self.by_id['G2']]))
        import_file(db, 'captain_sign', self._write('sign-v1.json', [self.by_id['K1']]))

    def test_frozen_baseline_blocks_departure_through_rules_calculate(self):
        db = connect(Path(self.tmp.name) / 'base.sqlite')
        self._base(db)
        decisions = report(db)['decisions']
        self.assertEqual(decisions['浅滩连续包络裁定']['result'], '冻结待补测')
        self.assertEqual(decisions['离港放行裁定']['result'], '阻断')
        self.assertEqual(decisions['航次潮窗核定']['result'], '开放')  # 旧窗口逻辑仍判开放
        self.assertTrue(audit(db)['ok'])
        db.close()

    def test_path_a_midchannel_gauges_keep_both_vehicles_and_release(self):
        db = connect(Path(self.tmp.name) / 'a.sqlite')
        self._base(db)
        # 路线甲：11:00 取得有效 220，并在每个包络结点补测，验证无未测低谷
        import_file(db, 'gauge', self._write('gauge-mid.json',
                                             [self.by_id['G3'], self.by_id['G4'], self.by_id['G5']]))
        rep = report(db)
        decisions = rep['decisions']
        self.assertEqual(decisions['浅滩连续包络裁定']['result'], '全曲线安全')
        self.assertEqual(decisions['离港放行裁定']['result'], '放行')
        # 两辆车原班运输：没有卸车重配账，证书清单仍为两车
        self.assertNotIn('卸车重配清单', decisions)
        self.assertEqual(certificate_history(db, 'F1')[-1]['manifest'], ['VIN1', 'VIN2'])
        safety = rep['channel_safety'][0]
        self.assertEqual(safety['verdict'], 'safe')
        self.assertGreaterEqual(safety['min_feasible_cm'], safety['required_level_cm'])
        # 历史冻结证书仍保留，新放行作为追加修订
        history = certificate_history(db, 'F1')
        self.assertIn('冻结待补测', [c['ruling'] for c in history])
        self.assertEqual(history[-1]['ruling'], '放行')
        self.assertTrue(audit(db)['ok'])
        db.close()

    def test_path_b_unload_heavy_vehicle_recomputes_everything_and_releases(self):
        db = connect(Path(self.tmp.name) / 'b.sqlite')
        self._base(db)
        # 路线乙：不等新水尺，卸下较重的 VIN1（1100 kg），余车 VIN2 重排中心线 L3
        import_file(db, 'restow', self._write('restow.json', [self.by_id['R1'], self.by_id['R2']]))
        # 版本 2 必须重新取得船长签认，否则不得离港
        before_sign = report(db)['decisions']
        self.assertEqual(before_sign['船长签名版本']['result'], '待签认')
        self.assertEqual(before_sign['离港放行裁定']['result'], '阻断')
        import_file(db, 'captain_sign', self._write('sign-v2.json', [self.by_id['K2']]))
        rep = report(db)
        d = rep['decisions']
        self.assertEqual(d['浅滩连续包络裁定']['result'], '全曲线安全')
        self.assertEqual(d['卸车重配清单']['result'], '重配完成')
        self.assertEqual(d['稳性裁定']['result'], '平衡')  # 中心线力臂 0
        self.assertEqual(d['原班候补改派']['result'], '重车进入下一班')
        self.assertEqual(d['船长签名版本']['result'], '已签认')
        self.assertEqual(d['离港放行裁定']['result'], '放行')
        # 吃水随实载重量降到 140，需求降到 160，最坏 170 仍安全
        safety = rep['channel_safety'][0]
        self.assertEqual(safety['draft_cm'], 140.0)
        self.assertEqual(safety['required_level_cm'], 160.0)
        self.assertEqual(safety['min_feasible_cm'], 170.0)
        # 实际运车清单只剩余车 VIN2，重车 VIN1 不登船（证书固化清单）
        cert = certificate_history(db, 'F1')[-1]
        self.assertEqual(cert['manifest'], ['VIN2'])
        reason = d['卸车重配清单']['reason']
        self.assertIn('VIN2', reason)
        self.assertIn("卸下 ['VIN1']", reason)
        self.assertTrue(audit(db)['ok'])
        db.close()

    def test_two_paths_yield_different_manifests_and_responsibilities(self):
        # 同一冻结输入，两条恢复链给出不同的实际运车清单与班次责任
        db_a = connect(Path(self.tmp.name) / 'cmp-a.sqlite')
        self._base(db_a)
        import_file(db_a, 'gauge', self._write('ga.json',
                                               [self.by_id['G3'], self.by_id['G4'], self.by_id['G5']]))
        a = report(db_a)['decisions']
        db_b = connect(Path(self.tmp.name) / 'cmp-b.sqlite')
        self._base(db_b)
        import_file(db_b, 'restow', self._write('rb.json', [self.by_id['R1'], self.by_id['R2']]))
        import_file(db_b, 'captain_sign', self._write('kb.json', [self.by_id['K2']]))
        b = report(db_b)['decisions']
        # 甲路线无改派、两车原班；乙路线重车进入下一班、原班只运余车
        self.assertNotIn('原班候补改派', a)
        self.assertEqual(b['原班候补改派']['result'], '重车进入下一班')
        self.assertNotIn('卸车重配清单', a)
        self.assertEqual(b['卸车重配清单']['result'], '重配完成')
        # 两条路线都满足最坏潮位与稳性硬约束并放行
        self.assertEqual(a['离港放行裁定']['result'], '放行')
        self.assertEqual(b['离港放行裁定']['result'], '放行')
        db_a.close()
        db_b.close()

    def test_late_gauge_replay_does_not_overwrite_historical_certificate(self):
        db = connect(Path(self.tmp.name) / 'late.sqlite')
        self._base(db)
        frozen_revision_certs = [c for c in report(db)['certificates'] if c['ruling'] == '冻结待补测']
        self.assertTrue(frozen_revision_certs)
        # 晚到的水尺原件按当时班次与装载版本回放（同一 gauge 事件幂等）
        again = import_file(db, 'gauge', self._write('gauge-end-copy.json',
                                                      [self.by_id['G1'], self.by_id['G2']]))
        self.assertTrue(again['idempotent'])
        import_file(db, 'gauge', self._write('gauge-mid-late.json',
                                             [self.by_id['G3'], self.by_id['G4'], self.by_id['G5']]))
        history = certificate_history(db, 'F1')
        rulings = [c['ruling'] for c in history]
        # 历史冻结证书没有被晚到原件覆盖
        self.assertIn('冻结待补测', rulings)
        self.assertEqual(rulings[-1], '放行')
        # 每条证书自带输入指纹与曲线见证，可逐条审计
        for c in history:
            self.assertEqual(len(c['input_fingerprint']), 64)
            self.assertTrue(c['witness_low'])
            self.assertTrue(c['witness_high'])
        self.assertTrue(audit(db)['ok'])
        db.close()

    def test_inconsistent_gauge_freezes_and_is_auditable(self):
        db = connect(Path(self.tmp.name) / 'bad.sqlite')
        self._base(db)
        # 一份与包络矛盾的水尺（包络下界 170，读数 90 且无其他 pin 能救回）
        rogue = dict(self.by_id['G4'], gauge_id='GAUGE-ROGUE', level_cm=90)
        import_file(db, 'gauge', self._write('rogue.json', [rogue]))
        decisions = report(db)['decisions']
        self.assertEqual(decisions['浅滩连续包络裁定']['result'], '冻结待补测')
        self.assertIn('矛盾', decisions['浅滩连续包络裁定']['reason'])
        self.assertEqual(decisions['离港放行裁定']['result'], '阻断')
        self.assertTrue(audit(db)['ok'])
        db.close()

    def test_new_event_kinds_have_independent_business_records(self):
        db = connect(Path(self.tmp.name) / 'rec.sqlite')
        self._base(db)
        import_file(db, 'restow', self._write('r.json', [self.by_id['R1'], self.by_id['R2']]))
        records = report(db)['business_records']
        subsystems = {r['subsystem'] for r in records}
        for expected in ('水尺采录账', '通道包络账', '配载与航行账', '船长签认账'):
            self.assertIn(expected, subsystems)
        # 水尺原件的证据条目带时间戳与读数，可独立审计
        gauge_rows = [r for r in records if r['subsystem'] == '水尺采录账']
        self.assertTrue(gauge_rows)
        self.assertTrue(all(json.loads(r['body']).get('observed_at') for r in gauge_rows))
        db.close()


if __name__ == '__main__':
    unittest.main()
