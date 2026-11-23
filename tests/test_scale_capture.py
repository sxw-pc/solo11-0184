"""Shared zero-drift bias certificates for weighbridge tickets.

F3 (fixtures/scale-shared-drift.json): two boarded vehicles on the same scale
in the same calibration period read 1100 and 1000 kg at lever arms -100/+100
cm.  Calibration only bounds the common zero bias b to [0,100] kg, so the true
weights are 1100-b and 1000-b and the center of gravity is pinned to
-10000/(2100-2b) cm — always inside [-6,+6] cm.  Treating the two tickets as
independent intervals would invent a -10 cm imbalance and freeze the sailing.
"""
import json
import tempfile
import unittest
from pathlib import Path

from ferryload.scale_capture import ScaleBook
from ferryload.store import connect, import_file, import_bundle, report, audit

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures'


def write_tmp(directory, events):
    path = Path(directory) / (f'events-{len(list(Path(directory).glob("events-*")))}.json')
    path.write_text(json.dumps(events, ensure_ascii=False), encoding='utf-8')
    return path


class ScaleCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(':memory:')
        import_bundle(self.db, FIXTURES / 'scale-shared-drift.json')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def add(self, kind, events):
        return import_file(self.db, kind, write_tmp(self.tmp.name, events))

    def decisions(self):
        return report(self.db)['decisions']

    def certificate(self, voyage='F3'):
        return report(self.db)['certificates'][voyage]

    # --- shared bias vs independent intervals ---------------------------------

    def test_shared_bias_certificate_bounds(self):
        cert = self.certificate()
        self.assertEqual(len(cert['bias_groups']), 1)
        group = cert['bias_groups'][0]
        self.assertEqual(group['key'], 'SCALE-1|CAL-1-CUR')
        self.assertEqual(group['bias_kg'], [0, 100])
        self.assertEqual([t['ticket_no'] for t in group['tickets']], ['T-3001', 'T-3002'])
        weight = cert['confirmed_weight_kg']
        self.assertEqual((weight['lower'], weight['upper']), (1900.0, 2100.0))
        center = cert['center_of_gravity_cm']
        self.assertAlmostEqual(center['lower'], -10000 / 1900, places=9)
        self.assertAlmostEqual(center['upper'], -10000 / 2100, places=9)
        self.assertEqual(center['status'], 'within')
        self.assertEqual(cert['quota']['status'], 'within')
        decisions = self.decisions()
        self.assertEqual(decisions['稳性裁定']['result'], '平衡')
        self.assertEqual(decisions['舱道分区额度']['result'], '有余量')
        self.assertEqual(decisions['离港放行裁定']['result'], '放行')
        self.assertEqual(decisions['共享偏差证书:F3']['result'], '已签发')
        self.assertTrue(audit(self.db)['ok'])

    def test_witnesses_attain_bounds_with_single_bias(self):
        center = self.certificate()['center_of_gravity_cm']
        low = center['lower_witness']
        self.assertEqual(low['bias'], {'SCALE-1|CAL-1-CUR': 100})
        self.assertEqual(low['ticket_weights'], {'T-3001': 1000.0, 'T-3002': 900.0})
        high = center['upper_witness']
        self.assertEqual(high['bias'], {'SCALE-1|CAL-1-CUR': 0})
        self.assertEqual(high['ticket_weights'], {'T-3001': 1100.0, 'T-3002': 1000.0})

    def test_independent_interval_combination_is_counterexample(self):
        # naive per-ticket intervals: w1 in [1000,1100], w2 in [900,1000];
        # combining one ticket's upper with the other's lower invents -10 cm
        naive_center = (-100 * 1100 + 100 * 900) / (1100 + 900)
        self.assertEqual(naive_center, -10.0)
        self.assertLess(naive_center, -6)  # would wrongly freeze under independence
        center = self.certificate()['center_of_gravity_cm']
        self.assertGreater(center['lower'], -6)  # shared b forbids the combination
        for witness in (center['lower_witness'], center['upper_witness']):
            weights = witness['ticket_weights']
            self.assertFalse(weights['T-3001'] == 1100.0 and weights['T-3002'] == 900.0,
                             'upper of one ticket may never combine with lower of the other')
            # every witness is explainable by one shared bias value
            bias = next(iter(witness['bias'].values()))
            self.assertEqual(weights['T-3001'], 1100 - bias)
            self.assertEqual(weights['T-3002'], 1000 - bias)

    def test_third_vehicle_other_period_judged_separately(self):
        decisions = self.decisions()
        self.assertEqual(decisions['候补递补资格']['result'], '候补待空位:VIN_F3')
        self.assertEqual(decisions['实测重量裁定:VIN_F3']['result'], '有效')
        # the waitlist ticket belongs to a different calibration period and must
        # not be merged into the shared variable b of CAL-1-CUR
        cert = self.certificate()
        self.assertEqual([g['key'] for g in cert['bias_groups']], ['SCALE-1|CAL-1-CUR'])
        events = [json.loads(row[0]) for row in self.db.execute('SELECT body FROM events ORDER BY seq')]
        book = ScaleBook.from_events(events)
        t3 = book.ticket('T-3003')
        self.assertEqual(book.group_key(t3), ('SCALE-1', 'CAL-1-PREV'))
        self.assertNotEqual(book.group_key(t3), book.group_key(book.ticket('T-3001')))
        self.assertTrue(book.ticket_valid(t3))

    # --- duplicates, revisions, voids ------------------------------------------

    def test_duplicate_ticket_does_not_add_weight(self):
        before = report(self.db)
        again = import_bundle(self.db, FIXTURES / 'scale-shared-drift.json')
        self.assertTrue(all(r['idempotent'] for r in again.values()))
        self.assertEqual(before, report(self.db))
        # same ticket re-captured under a new event id with identical content
        self.add('scale', [dict(DUPLICATE_TICKET)])
        after = report(self.db)
        self.assertEqual(before['decisions'], after['decisions'])
        self.assertEqual(before['certificates'], after['certificates'])
        weight = after['certificates']['F3']['confirmed_weight_kg']
        self.assertEqual((weight['lower'], weight['upper']), (1900.0, 2100.0))

    def test_conflicting_ticket_revision_rolls_back(self):
        before = report(self.db)
        bad = dict(DUPLICATE_TICKET, id='E2-conflict', reading_kg=1200)
        with self.assertRaises(ValueError):
            self.add('scale', [bad])
        self.assertEqual(before, report(self.db))

    def test_void_revision_rebuilds_voyage(self):
        self.add('scale', [{'id': 'E2V2', 'kind': 'scale', 'role': 'ticket',
                            'ticket_no': 'T-3002', 'revision': 2, 'void': True, 'version': 2}])
        decisions = self.decisions()
        self.assertEqual(decisions['实测重量裁定:VIN_F2']['result'], '待复称')
        self.assertEqual(decisions['称重校准可信度:VIN_F2']['result'], '失效')
        self.assertEqual(decisions['稳性裁定']['result'], '失衡或无实载')
        self.assertEqual(decisions['离港放行裁定']['result'], '阻断')
        cert = self.certificate()
        self.assertEqual(list(cert['generated_from']['tickets']), ['T-3001'])
        history = report(self.db)['certificate_history']
        self.assertGreater(len([h for h in history if h['voyage_id'] == 'F3']), 1)
        self.assertTrue(audit(self.db)['ok'])

    # --- the two legal follow-up paths -----------------------------------------

    def test_calibration_errata_narrows_shared_bias(self):
        self.add('scale', [dict(ERRATA_CALIBRATION)])
        cert = self.certificate()
        group = cert['bias_groups'][0]
        self.assertEqual(group['bias_kg'], [40, 60])
        self.assertEqual(group['calibration_version'], 2)
        center = cert['center_of_gravity_cm']
        self.assertAlmostEqual(center['lower'], -10000 / 1980, places=9)
        self.assertAlmostEqual(center['upper'], -10000 / 2020, places=9)
        decisions = self.decisions()
        self.assertEqual(decisions['稳性裁定']['result'], '平衡')
        self.assertEqual(decisions['离港放行裁定']['result'], '放行')
        # the old certificate and its calibration provenance are retained
        history = report(self.db)['certificate_history']
        versions = {h['body']['generated_from']['calibrations']['CAL-1-CUR']['version']
                    for h in history
                    if h['voyage_id'] == 'F3'
                    and 'CAL-1-CUR' in h['body']['generated_from']['calibrations']}
        self.assertEqual(versions, {1, 2})
        self.assertTrue(audit(self.db)['ok'])

    def test_conflicting_calibration_version_rejected(self):
        before = report(self.db)
        bad = dict(ERRATA_CALIBRATION, id='K1-bad', version=1)
        with self.assertRaises(ValueError):
            self.add('scale', [bad])
        self.assertEqual(before, report(self.db))

    def test_independent_scale_reweigh_recomputes_association(self):
        self.add('scale', [dict(REWEIGH_CALIBRATION), dict(REWEIGH_TICKET)])
        self.add('vehicle', [dict(REWEIGH_VEHICLE)])
        cert = self.certificate()
        self.assertEqual([g['key'] for g in cert['bias_groups']],
                         ['SCALE-1|CAL-1-CUR', 'SCALE-2|CAL-2-CERT'])
        center = cert['center_of_gravity_cm']
        # with VIN_F1 pinned to exactly 1100 kg by the certified scale, the
        # remaining drift of T-3002 makes the -10 cm imbalance genuinely possible
        self.assertAlmostEqual(center['lower'], -10.0, places=9)
        self.assertAlmostEqual(center['upper'], -10000 / 2100, places=9)
        self.assertEqual(center['status'], 'straddling')
        decisions = self.decisions()
        self.assertEqual(decisions['稳性裁定']['result'], '边界待定')
        self.assertEqual(decisions['离港放行裁定']['result'], '阻断')
        events = [json.loads(row[0]) for row in self.db.execute('SELECT body FROM events ORDER BY seq')]
        book = ScaleBook.from_events(events)
        self.assertIsNone(book.ticket('T-3001'))  # superseded by the reweigh
        self.assertTrue(audit(self.db)['ok'])

    # --- midpoint trap and boundaries ------------------------------------------

    def test_midpoint_would_misjudge_independent_groups(self):
        for kind, events in INDEPENDENT_SCENARIO.items():
            self.add(kind, events)
        cert = self.certificate('F9')
        self.assertEqual([g['key'] for g in cert['bias_groups']], ['SA|CA', 'SB|CB'])
        center = cert['center_of_gravity_cm']
        self.assertAlmostEqual(center['lower'], -10.0, places=9)
        self.assertAlmostEqual(center['upper'], 0.0, places=9)
        # midpoint of each independent interval gives -5 cm: inside the limits,
        # yet the true vertex range straddles -6 cm and must not pass
        midpoint_center = (-100 * 1050 + 100 * 950) / 2000
        self.assertTrue(-6 <= midpoint_center <= 6)
        decisions = self.decisions()
        self.assertEqual(decisions['稳性裁定']['result'], '边界待定')
        self.assertEqual(decisions['离港放行裁定']['result'], '阻断')
        self.assertTrue(audit(self.db)['ok'])

    def test_center_boundary_is_inclusive(self):
        for kind, events in BOUNDARY_SCENARIO.items():
            self.add(kind, events)
        center = self.certificate('F8')['center_of_gravity_cm']
        self.assertEqual((center['lower'], center['upper']), (-6.0, -6.0))
        self.assertEqual(center['status'], 'within')
        decisions = self.decisions()
        self.assertEqual(decisions['稳性裁定']['result'], '平衡')
        self.assertEqual(decisions['离港放行裁定']['result'], '放行')

    def test_weighed_outside_calibration_window_invalid(self):
        for kind, events in EXPIRED_SCENARIO.items():
            self.add(kind, events)
        decisions = self.decisions()
        self.assertEqual(decisions['实测重量裁定:VIN_W1']['result'], '待复称')
        self.assertEqual(decisions['称重校准可信度:VIN_W1']['result'], '失效')

    # --- capture completeness and legacy regression -----------------------------

    def test_ticket_identity_and_ocr_captured(self):
        records = report(self.db)['business_records']
        tickets = {r['entity_id']: json.loads(r['body']) for r in records
                   if r['subsystem'] == '磅票与校准账' and r['entity_type'] == '磅票'}
        self.assertEqual(tickets['T-3001']['ocr'], {'page': 1, 'box': [12, 34, 120, 18]})
        self.assertEqual(tickets['T-3001']['reading_kg'], 1100)
        self.assertEqual(tickets['T-3001']['calibration_id'], 'CAL-1-CUR')
        calibrations = {r['entity_id']: json.loads(r['body']) for r in records
                        if r['subsystem'] == '磅票与校准账' and r['entity_type'] == '校准记录'}
        self.assertEqual(calibrations['K1']['zero_bias_kg'], [0, 100])
        self.assertEqual(len(calibrations['K1']['points']), 2)
        cert = self.certificate()
        self.assertEqual(cert['generated_from']['calibrations'],
                         {'CAL-1-CUR': {'event': 'K1', 'version': 1}})
        self.assertEqual(cert['generated_from']['tickets']['T-3002'],
                         {'event': 'E2', 'revision': 1, 'reading_kg': 1000.0,
                          'bias_group': 'SCALE-1|CAL-1-CUR'})

    def test_legacy_fixtures_output_unchanged(self):
        legacy = connect(':memory:')
        try:
            for kind, name in [('voyage', 'voyage-ok.json'), ('vehicle', 'vehicle-ok.json'),
                               ('stow', 'stow-ok.json')]:
                import_file(legacy, kind, FIXTURES / name)
            self.assertEqual(report(legacy)['decisions'], LEGACY_DECISIONS)
        finally:
            legacy.close()


DUPLICATE_TICKET = {'id': 'E2-dup', 'kind': 'scale', 'role': 'ticket', 'ticket_no': 'T-3002',
                    'vin': 'VIN_F2', 'voyage_id': 'F3', 'scale_id': 'SCALE-1',
                    'calibration_id': 'CAL-1-CUR', 'reading_kg': 1000,
                    'weighed_at': '2026-07-10T09:05:00Z', 'revision': 1, 'void': False,
                    'ocr': {'page': 1, 'box': [12, 58, 120, 18]}, 'version': 1}

ERRATA_CALIBRATION = {'id': 'K1V2', 'kind': 'scale', 'role': 'calibration', 'scale_id': 'SCALE-1',
                      'calibration_id': 'CAL-1-CUR', 'valid_from': '2026-06-01T00:00:00Z',
                      'valid_to': '2026-08-01T00:00:00Z',
                      'points': [{'known_kg': 0, 'indicated_kg': 50},
                                 {'known_kg': 2000, 'indicated_kg': 2050}],
                      'zero_bias_kg': [40, 60], 'version': 2}

REWEIGH_CALIBRATION = {'id': 'K9', 'kind': 'scale', 'role': 'calibration', 'scale_id': 'SCALE-2',
                       'calibration_id': 'CAL-2-CERT', 'valid_from': '2026-06-01T00:00:00Z',
                       'valid_to': '2026-09-01T00:00:00Z',
                       'points': [{'known_kg': 0, 'indicated_kg': 0},
                                  {'known_kg': 2000, 'indicated_kg': 2000}],
                       'zero_bias_kg': [0, 0], 'version': 1}

REWEIGH_TICKET = {'id': 'E9', 'kind': 'scale', 'role': 'ticket', 'ticket_no': 'T-3004',
                  'vin': 'VIN_F1', 'voyage_id': 'F3', 'scale_id': 'SCALE-2',
                  'calibration_id': 'CAL-2-CERT', 'reading_kg': 1100,
                  'weighed_at': '2026-07-11T09:00:00Z', 'revision': 1, 'void': False,
                  'supersedes': 'T-3001', 'ocr': {'page': 3, 'box': [10, 20, 110, 16]}, 'version': 1}

REWEIGH_VEHICLE = {'id': 'D1V2', 'kind': 'vehicle', 'voyage_id': 'F3', 'vin': 'VIN_F1',
                   'scan_vin': 'VIN_F1', 'declared_kg': 1050, 'weigh_ticket': 'T-3004',
                   'length_cm': 450, 'height_cm': 190, 'hazmat_declared': True,
                   'hazmat_class': None, 'version': 2}

INDEPENDENT_SCENARIO = {
    'voyage': [{'id': 'V9', 'kind': 'voyage', 'voyage_id': 'F9',
                'departure_at': '2026-07-15T10:00:00Z', 'required_level_cm': 180,
                'tide_windows': [{'start': '2026-07-15T08:00:00Z', 'end': '2026-07-15T12:00:00Z',
                                  'level_cm': 220}],
                'cancelled': False, 'captain_signed': True,
                'lanes': {'X': {'weight_kg': 2000, 'length_cm': 600, 'height_cm': 300, 'lever_arm_cm': -100},
                          'Y': {'weight_kg': 2000, 'length_cm': 600, 'height_cm': 300, 'lever_arm_cm': 100}},
                'stability_min_cm': -6, 'stability_max_cm': 6, 'version': 1}],
    'vehicle': [{'id': 'M1', 'kind': 'vehicle', 'voyage_id': 'F9', 'vin': 'VIN_M1', 'scan_vin': 'VIN_M1',
                 'declared_kg': 1050, 'weigh_ticket': 'TM1', 'length_cm': 450, 'height_cm': 190,
                 'hazmat_declared': True, 'hazmat_class': None, 'version': 1},
                {'id': 'M2', 'kind': 'vehicle', 'voyage_id': 'F9', 'vin': 'VIN_M2', 'scan_vin': 'VIN_M2',
                 'declared_kg': 950, 'weigh_ticket': 'TM2', 'length_cm': 450, 'height_cm': 190,
                 'hazmat_declared': True, 'hazmat_class': None, 'version': 1}],
    'stow': [{'id': 'SM1', 'kind': 'stow', 'voyage_id': 'F9', 'vin': 'VIN_M1', 'lane': 'X',
              'boarded': True, 'unloaded': True, 'version': 1},
             {'id': 'SM2', 'kind': 'stow', 'voyage_id': 'F9', 'vin': 'VIN_M2', 'lane': 'Y',
              'boarded': True, 'unloaded': True, 'version': 1}],
    'scale': [{'id': 'KC1', 'kind': 'scale', 'role': 'calibration', 'scale_id': 'SA',
               'calibration_id': 'CA', 'valid_from': '2026-06-01T00:00:00Z',
               'valid_to': '2026-08-01T00:00:00Z',
               'points': [{'known_kg': 0, 'indicated_kg': 0}, {'known_kg': 2000, 'indicated_kg': 2100}],
               'zero_bias_kg': [0, 100], 'version': 1},
              {'id': 'KC2', 'kind': 'scale', 'role': 'calibration', 'scale_id': 'SB',
               'calibration_id': 'CB', 'valid_from': '2026-06-01T00:00:00Z',
               'valid_to': '2026-08-01T00:00:00Z',
               'points': [{'known_kg': 0, 'indicated_kg': 0}, {'known_kg': 2000, 'indicated_kg': 2100}],
               'zero_bias_kg': [0, 100], 'version': 1},
              {'id': 'EM1', 'kind': 'scale', 'role': 'ticket', 'ticket_no': 'TM1', 'vin': 'VIN_M1',
               'voyage_id': 'F9', 'scale_id': 'SA', 'calibration_id': 'CA', 'reading_kg': 1100,
               'weighed_at': '2026-07-10T09:00:00Z', 'revision': 1, 'void': False,
               'ocr': {'page': 1, 'box': [1, 1, 1, 1]}, 'version': 1},
              {'id': 'EM2', 'kind': 'scale', 'role': 'ticket', 'ticket_no': 'TM2', 'vin': 'VIN_M2',
               'voyage_id': 'F9', 'scale_id': 'SB', 'calibration_id': 'CB', 'reading_kg': 1000,
               'weighed_at': '2026-07-10T09:00:00Z', 'revision': 1, 'void': False,
               'ocr': {'page': 1, 'box': [1, 1, 1, 1]}, 'version': 1}]}

BOUNDARY_SCENARIO = {
    'voyage': [{'id': 'V8', 'kind': 'voyage', 'voyage_id': 'F8',
                'departure_at': '2026-07-15T10:00:00Z', 'required_level_cm': 180,
                'tide_windows': [{'start': '2026-07-15T08:00:00Z', 'end': '2026-07-15T12:00:00Z',
                                  'level_cm': 220}],
                'cancelled': False, 'captain_signed': True,
                'lanes': {'X': {'weight_kg': 2000, 'length_cm': 600, 'height_cm': 300, 'lever_arm_cm': -6}},
                'stability_min_cm': -6, 'stability_max_cm': 6, 'version': 1}],
    'vehicle': [{'id': 'B1', 'kind': 'vehicle', 'voyage_id': 'F8', 'vin': 'VIN_B1', 'scan_vin': 'VIN_B1',
                 'declared_kg': 1000, 'weigh_ticket': 'TB1', 'length_cm': 450, 'height_cm': 190,
                 'hazmat_declared': True, 'hazmat_class': None, 'version': 1}],
    'stow': [{'id': 'SB1', 'kind': 'stow', 'voyage_id': 'F8', 'vin': 'VIN_B1', 'lane': 'X',
              'boarded': True, 'unloaded': True, 'version': 1}],
    'scale': [{'id': 'KB', 'kind': 'scale', 'role': 'calibration', 'scale_id': 'SA',
               'calibration_id': 'CA', 'valid_from': '2026-06-01T00:00:00Z',
               'valid_to': '2026-08-01T00:00:00Z',
               'points': [{'known_kg': 0, 'indicated_kg': 0}, {'known_kg': 2000, 'indicated_kg': 2100}],
               'zero_bias_kg': [0, 100], 'version': 1},
              {'id': 'EB1', 'kind': 'scale', 'role': 'ticket', 'ticket_no': 'TB1', 'vin': 'VIN_B1',
               'voyage_id': 'F8', 'scale_id': 'SA', 'calibration_id': 'CA', 'reading_kg': 1100,
               'weighed_at': '2026-07-10T09:00:00Z', 'revision': 1, 'void': False,
               'ocr': {'page': 1, 'box': [1, 1, 1, 1]}, 'version': 1}]}

EXPIRED_SCENARIO = {
    'voyage': [{'id': 'V7', 'kind': 'voyage', 'voyage_id': 'F7',
                'departure_at': '2026-07-15T10:00:00Z', 'required_level_cm': 180,
                'tide_windows': [{'start': '2026-07-15T08:00:00Z', 'end': '2026-07-15T12:00:00Z',
                                  'level_cm': 220}],
                'cancelled': False, 'captain_signed': True,
                'lanes': {'X': {'weight_kg': 2000, 'length_cm': 600, 'height_cm': 300, 'lever_arm_cm': 0}},
                'stability_min_cm': -6, 'stability_max_cm': 6, 'version': 1}],
    'vehicle': [{'id': 'W1', 'kind': 'vehicle', 'voyage_id': 'F7', 'vin': 'VIN_W1', 'scan_vin': 'VIN_W1',
                 'declared_kg': 1000, 'weigh_ticket': 'TW1', 'length_cm': 450, 'height_cm': 190,
                 'hazmat_declared': True, 'hazmat_class': None, 'version': 1}],
    'scale': [{'id': 'KW', 'kind': 'scale', 'role': 'calibration', 'scale_id': 'SA',
               'calibration_id': 'CA', 'valid_from': '2026-06-01T00:00:00Z',
               'valid_to': '2026-07-01T00:00:00Z',
               'points': [{'known_kg': 0, 'indicated_kg': 0}, {'known_kg': 2000, 'indicated_kg': 2100}],
               'zero_bias_kg': [0, 100], 'version': 1},
              {'id': 'EW1', 'kind': 'scale', 'role': 'ticket', 'ticket_no': 'TW1', 'vin': 'VIN_W1',
               'voyage_id': 'F7', 'scale_id': 'SA', 'calibration_id': 'CA', 'reading_kg': 1100,
               'weighed_at': '2026-07-10T09:00:00Z', 'revision': 1, 'void': False,
               'ocr': {'page': 1, 'box': [1, 1, 1, 1]}, 'version': 1}]}

LEGACY_DECISIONS = {'候补递补资格': {'evidence': ['V1'], 'reason': '合格而未占舱位的预约按优先级、舱道尺寸和危险品隔离递补', 'result': '无候补'},
 '停航退票责任': {'evidence': ['V1'], 'reason': '只对停航且未实际登船的预约生成责任', 'result': '无退票'},
 '危险品隔离核验:VIN1': {'evidence': ['C1'], 'reason': '危险品须如实申报并隔离', 'result': '合格'},
 '危险品隔离核验:VIN2': {'evidence': ['C3'], 'reason': '危险品须如实申报并隔离', 'result': '合格'},
 '实测重量裁定:VIN1': {'evidence': ['C1'], 'reason': '校准有效的实测重量参与全船额度', 'result': '有效'},
 '实测重量裁定:VIN2': {'evidence': ['C3'], 'reason': '校准有效的实测重量参与全船额度', 'result': '有效'},
 '实载额度释放': {'evidence': ['V1', 'S1', 'S3'], 'reason': '只将已经登船的车辆计入实载占用；缺扫描的原计划释放可用额度', 'result': '0条空舱道'},
 '潮窗冲突冻结': {'evidence': ['V1'], 'reason': '冲突潮窗不得继续开放', 'result': '正常'},
 '登船与离船闭环': {'evidence': ['S1', 'S3', 'V1'], 'reason': '每辆已配载车应有登船及到港离船扫描', 'result': '闭环'},
 '离港放行裁定': {'evidence': ['V1', 'S1', 'S3'], 'reason': '潮窗、重量、配载、扫描和船长签认共同决定', 'result': '放行'},
 '称重校准可信度:VIN1': {'evidence': ['C1'], 'reason': '校准失效的重量不应被签认', 'result': '可信'},
 '称重校准可信度:VIN2': {'evidence': ['C3'], 'reason': '校准失效的重量不应被签认', 'result': '可信'},
 '稳性裁定': {'evidence': ['V1', 'S1', 'S3'], 'reason': '实载总重 1950 千克，横向重心 -12.8 厘米', 'result': '平衡'},
 '航次潮窗核定': {'evidence': ['V1'], 'reason': '潮窗有效、无冲突且未撤销', 'result': '开放'},
 '舱位配载确认': {'evidence': ['V1', 'S1', 'S3'], 'reason': '舱位唯一且航次潮窗、额度和状态仍有效', 'result': '确认'},
 '舱道分区额度': {'evidence': ['V1', 'C1', 'C3'], 'reason': '有效实测总重与各舱道额定重量比较', 'result': '有余量'},
 '车辆身份复核:VIN1': {'evidence': ['C1'], 'reason': '预约 VIN 必须与闸口扫描一致', 'result': '通过'},
 '车辆身份复核:VIN2': {'evidence': ['C3'], 'reason': '预约 VIN 必须与闸口扫描一致', 'result': '通过'}}


if __name__ == '__main__':
    unittest.main()
