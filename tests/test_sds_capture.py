import json
import unittest
from pathlib import Path

from ferryload import sds_capture
from ferryload.store import connect, import_bundle, import_payload, report, audit

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'


def load_bundle():
    return json.loads((FIXTURES / 'sds-threeway.json').read_text(encoding='utf-8'))


def without_sds(bundle):
    return {kind: events for kind, events in bundle.items() if kind != 'sds'}


class SdsCaptureTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')
        self.bundle = load_bundle()

    def tearDown(self):
        self.db.close()

    def result(self, name):
        return report(self.db)['decisions'][name]['result']

    def reason(self, name):
        return report(self.db)['decisions'][name]['reason']

    def graph(self):
        return sds_capture.build_graph(self.bundle['sds'], vehicles=self.bundle['vehicle'])

    def test_ternary_hyperedge_blocks_and_any_pair_legal(self):
        voyage = self.bundle['voyage'][0]
        # 夹具固定：L1/L2/L3 互不 adjacent，却共用通风区 VENT-A
        self.assertTrue(all(not voyage['adjacent_lanes'][lane] for lane in ('L1', 'L2', 'L3')))
        self.assertEqual(voyage['ventilation_zones']['VENT-A'], ['L1', 'L2', 'L3'])
        import_bundle(self.db, self.bundle)
        self.assertEqual(self.result('SDS禁配超边核验:F2'), '禁配')
        reason = self.reason('SDS禁配超边核验:F2')
        for token in ('VINA', 'VINB', 'VINC', 'VENT-A', 'R-3WAY', '不相邻'):
            self.assertIn(token, reason)
        self.assertEqual(self.result('舱位配载确认'), '待改配')
        self.assertEqual(self.result('离港放行裁定'), '阻断')
        for vin in ('VINA', 'VINB', 'VINC'):
            self.assertEqual(self.result(f'危险品隔离核验:{vin}'), '拒载')
            self.assertIn('元禁配超边', self.reason(f'危险品隔离核验:{vin}'))
        self.assertTrue(audit(self.db)['ok'])
        # 图级核验：最小冲突集恰为 {A,B,C}，任意两者同区均合法
        graph = self.graph()
        vehicles = {x['vin']: x for x in self.bundle['vehicle']}
        plans = self.bundle['stow']
        hits = sds_capture.violations(graph, plans, vehicles, voyage)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['conflict'], ['VINA', 'VINB', 'VINC'])
        self.assertTrue(hits[0]['non_adjacent'])
        for dropped in ('VINA', 'VINB', 'VINC'):
            pair = [p for p in plans if p['vin'] != dropped]
            self.assertEqual(sds_capture.violations(graph, pair, vehicles, voyage), [], dropped)

    def test_conventional_pipeline_finds_nothing_without_sds(self):
        # 完整常规流程（身份、校准称重、潮窗、稳性、两两邻接矩阵、舱道容量、SQLite 原件事务）
        # 全部通过也找不到违反的边：任意一对合法且舱道互不 adjacent
        import_bundle(self.db, without_sds(self.bundle))
        self.assertEqual(self.result('舱位配载确认'), '确认')
        self.assertEqual(self.result('离港放行裁定'), '放行')
        self.assertEqual(self.result('危险品隔离核验:VINA'), '合格')
        # 危货车辆缺少 SDS 采录时不得由两两表未列名推断组合安全
        self.assertEqual(self.result('SDS禁配超边核验:F2'), '待核')
        again = import_bundle(self.db, without_sds(self.bundle))
        self.assertTrue(all(item['idempotent'] for item in again.values()))
        # 同一配载表，导入 SDS 原件后被三元超边阻断
        import_payload(self.db, 'sds', self.bundle['sds'])
        self.assertEqual(self.result('SDS禁配超边核验:F2'), '禁配')
        self.assertEqual(self.result('舱位配载确认'), '待改配')
        self.assertEqual(self.result('离港放行裁定'), '阻断')
        self.assertIn('不相邻', self.reason('SDS禁配超边核验:F2'))
        self.assertTrue(audit(self.db)['ok'])

    def test_restow_to_independent_zone_resolves(self):
        import_bundle(self.db, self.bundle)
        self.assertEqual(self.result('离港放行裁定'), '阻断')
        # 路径一：C 改配到经检验的独立通风分区 VENT-B 的 L4，三车仍同航次
        import_payload(self.db, 'stow', [
            {'id': 'SC2', 'kind': 'stow', 'voyage_id': 'F2', 'vin': 'VINC',
             'lane': 'L4', 'boarded': True, 'unloaded': False, 'version': 2}])
        self.assertEqual(self.result('SDS禁配超边核验:F2'), '合格')
        # 实载清单已变，旧配载签认不得暗中沿用
        self.assertEqual(self.result('配载清单签认:F2'), '待重签')
        self.assertEqual(self.result('离港放行裁定'), '阻断')
        # 船长重签新清单后放行
        voyage = dict(self.bundle['voyage'][0])
        voyage.update({'id': 'V11', 'version': 2,
                       'signed_manifest': [['VINA', 'L1'], ['VINB', 'L2'], ['VINC', 'L4']]})
        import_payload(self.db, 'voyage', [voyage])
        self.assertEqual(self.result('配载清单签认:F2'), '签认有效')
        self.assertEqual(self.result('舱位配载确认'), '确认')
        self.assertEqual(self.result('离港放行裁定'), '放行')
        self.assertIn('3000', self.reason('稳性裁定'))
        self.assertTrue(audit(self.db)['ok'])

    def test_waitlist_swap_resolves_and_changes_manifest(self):
        import_bundle(self.db, self.bundle)
        # 路径二：B 退到候补/下一航次，无危货候补车 D 递补原舱道 L2
        vehicle_b = dict(self.bundle['vehicle'][1])
        vehicle_b.update({'id': 'CB2', 'status': 'waitlist', 'priority': 1, 'version': 2})
        vehicle_d = dict(self.bundle['vehicle'][3])
        vehicle_d.update({'id': 'CD2', 'status': 'confirmed', 'version': 2})
        import_payload(self.db, 'vehicle', [vehicle_b, vehicle_d])
        import_payload(self.db, 'stow', [
            {'id': 'SB2', 'kind': 'stow', 'voyage_id': 'F2', 'vin': 'VINB', 'lane': 'L2',
             'boarded': False, 'unloaded': False, 'status': 'released', 'version': 2},
            {'id': 'SD1', 'kind': 'stow', 'voyage_id': 'F2', 'vin': 'VIND', 'lane': 'L2',
             'boarded': True, 'unloaded': False, 'version': 1}])
        self.assertEqual(self.result('配载清单签认:F2'), '待重签')
        self.assertEqual(self.result('离港放行裁定'), '阻断')
        voyage = dict(self.bundle['voyage'][0])
        voyage.update({'id': 'V12', 'version': 2,
                       'signed_manifest': [['VINA', 'L1'], ['VINC', 'L3'], ['VIND', 'L2']]})
        import_payload(self.db, 'voyage', [voyage])
        # 同一三元超边被消除，但实载 VIN、舱道占用与额度都不同
        self.assertEqual(self.result('SDS禁配超边核验:F2'), '合格')
        self.assertEqual(self.result('离港放行裁定'), '放行')
        self.assertEqual(self.result('实载额度释放'), '1条空舱道')
        self.assertEqual(self.result('候补递补资格'), '可递补:VINB')
        self.assertIn('2950', self.reason('稳性裁定'))
        entities = report(self.db)['entities']
        self.assertTrue(any(e['subsystem'] == '配载与航行账' and e['entity_id'] == 'VIND'
                            for e in entities))
        self.assertTrue(audit(self.db)['ok'])

    def test_ocr_negation_loss_flags_review(self):
        bundle = json.loads(json.dumps(self.bundle))
        page = bundle['sds'][0]['pages'][1]
        page['tokens'] = [token for token in page['tokens'] if token != '不得']
        import_bundle(self.db, bundle)
        # OCR 丢失否定词：来源文句不可信，输出待核而不是禁配
        self.assertEqual(self.result('SDS禁配超边核验:F2'), '待核')
        self.assertIn('否定词', self.reason('SDS禁配超边核验:F2'))
        self.assertNotEqual(self.result('SDS禁配超边核验:F2'), '禁配')
        self.assertTrue(audit(self.db)['ok'])

    def test_mixed_errata_pages_flag_review(self):
        bundle = json.loads(json.dumps(self.bundle))
        bundle['sds'][0]['pages'][1]['errata'] = 1  # 限制来源页混入旧版勘误
        import_bundle(self.db, bundle)
        self.assertEqual(self.result('SDS禁配超边核验:F2'), '待核')
        self.assertIn('旧版页', self.reason('SDS禁配超边核验:F2'))
        self.assertTrue(audit(self.db)['ok'])

    def test_concentration_ambiguity_flags_review(self):
        bundle = json.loads(json.dumps(self.bundle))
        bundle['sds'][0]['catalog'].append(
            {'chemical_code': 'UN2014', 'name': 'hydrogen peroxide', 'concentration': '60%'})
        del bundle['sds'][0]['restrictions'][0]['cargoes'][0]['concentration']
        import_bundle(self.db, bundle)
        self.assertEqual(self.result('SDS禁配超边核验:F2'), '待核')
        self.assertIn('浓度', self.reason('SDS禁配超边核验:F2'))
        self.assertTrue(audit(self.db)['ok'])

    def test_stale_batch_after_cargo_swap_flags_review(self):
        import_bundle(self.db, self.bundle)
        # 车辆换货：VINC 实载批次已变，SDS 仍按旧批次采录
        vehicle_c = dict(self.bundle['vehicle'][2])
        vehicle_c['cargo'] = dict(vehicle_c['cargo'], batch='BATCH-C2')
        vehicle_c.update({'id': 'CC2', 'version': 2})
        import_payload(self.db, 'vehicle', [vehicle_c])
        self.assertEqual(self.result('SDS禁配超边核验:F2'), '待核')
        self.assertIn('批次', self.reason('SDS禁配超边核验:F2'))
        self.assertIn('VINC', self.reason('SDS禁配超边核验:F2'))
        self.assertNotEqual(self.result('SDS禁配超边核验:F2'), '禁配')
        self.assertTrue(audit(self.db)['ok'])

    def test_late_errata_recalculates_and_keeps_traces(self):
        import_bundle(self.db, without_sds(self.bundle))
        # 航次按当时证据离港：登离船扫描闭环、船长签认有效
        departed = [dict(p, id=p['id'] + 'D', boarded=True, unloaded=True, version=2)
                    for p in self.bundle['stow']]
        import_payload(self.db, 'stow', departed)
        self.assertEqual(self.result('离港放行裁定'), '放行')
        # 旧勘误 v1：限制仅适用于 20°C 以下，本航次预报 25°C，判定合格
        sds0 = json.loads(json.dumps(self.bundle['sds'][0]))
        sds0.update({'id': 'SDS0', 'errata_version': 1, 'version': 1,
                     'captured_at': '2026-05-10T08:00:00Z'})
        for page in sds0['pages']:
            page['errata'] = 1
        restriction = sds0['restrictions'][0]
        restriction['errata_version'] = 1
        restriction['condition'] = {'temp_min_c': -10, 'temp_max_c': 20, 'transport_modes': ['sea']}
        restriction['text'] = 'v1原文：不得将三者同时存放于同一通风分区（仅适用于 20°C 以下）。'
        import_payload(self.db, 'sds', [sds0])
        self.assertEqual(self.result('SDS禁配超边核验:F2'), '合格')
        # 迟到勘误 v2（fixture 原件）把适用温度上限更正为 40°C：同一配载表改判禁配
        import_payload(self.db, 'sds', self.bundle['sds'])
        self.assertEqual(self.result('SDS禁配超边核验:F2'), '禁配')
        self.assertTrue(self.result('SDS勘误追溯:F2').startswith('留痕'))
        self.assertIn('已离港', self.reason('SDS勘误追溯:F2'))
        # 已离港清单与新禁配结论分别留痕，不以最终版覆盖当时来源文句
        history = {row[0] for row in self.db.execute(
            "SELECT result FROM decision_history WHERE name='SDS禁配超边核验:F2'")}
        self.assertIn('合格', history)
        self.assertIn('禁配', history)
        timeline = sds_capture.errata_timeline(
            sds_capture.build_graph([sds0] + self.bundle['sds']))
        texts = [row['text'] for row in timeline[('SDS-3WAY', 'R-3WAY')]]
        self.assertEqual(len(texts), 2)
        self.assertNotEqual(texts[0], texts[1])
        self.assertTrue(audit(self.db)['ok'])

    def test_graph_queries_hit_and_resolution(self):
        graph = self.graph()
        voyage = self.bundle['voyage'][0]
        vehicles = {x['vin']: x for x in self.bundle['vehicle']}
        plans = self.bundle['stow']
        hit = sds_capture.violations(graph, plans, vehicles, voyage)[0]
        options = sds_capture.resolutions(graph, hit, plans, vehicles, voyage)
        # 哪辆换区能消除冲突：C 可改配到独立通风分区 VENT-B 的 L4
        moves = {(m['vin'], m['to_lane'], m['to_zone']) for m in options['moves']}
        self.assertIn(('VINC', 'L4', 'VENT-B'), moves)
        self.assertTrue(all(zone != 'VENT-A' for _, _, zone in moves))
        # 哪辆下船能消除冲突：最小冲突集任一辆退到候补/下一航次即可
        self.assertEqual({o['vin'] for o in options['offloads']}, {'VINA', 'VINB', 'VINC'})
        moved = [dict(p, lane='L4') if p['vin'] == 'VINC' else p for p in plans]
        self.assertEqual(sds_capture.violations(graph, moved, vehicles, voyage), [])


if __name__ == '__main__':
    unittest.main()
