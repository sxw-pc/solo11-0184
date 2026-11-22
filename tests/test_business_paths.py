import unittest
from pathlib import Path
from ferryload.store import connect, import_file, report, audit

FIXTURES=Path(__file__).resolve().parents[1]/'fixtures'
class PathTests(unittest.TestCase):
    def setUp(self):
        self.db=connect(':memory:')
        for kind,name in [('voyage','voyage-ok.json'),('vehicle','vehicle-ok.json'),('stow','stow-ok.json')]:
            import_file(self.db,kind,FIXTURES/name)
    def tearDown(self): self.db.close()
    def add(self,kind,name): import_file(self.db,kind,FIXTURES/name)
    def result(self,name): return report(self.db)['decisions'][name]['result']
    def test_arrival_gap_preserves_departure_but_opens_closure(self):
        self.add('stow','stow-arrival-gap.json')
        self.assertEqual(self.result('离港放行裁定'),'放行')
        self.assertEqual(self.result('登船与离船闭环'),'缺口')
        self.assertTrue(audit(self.db)['ok'])
    def test_adjacent_hazardous_classes_block_departure(self):
        self.add('vehicle','vehicle-hazmat.json')
        self.assertEqual(self.result('危险品隔离核验:VIN1'),'拒载')
        self.assertEqual(self.result('离港放行裁定'),'阻断')
    def test_valid_waitlist_does_not_change_confirmed_manifest(self):
        self.add('vehicle','vehicle-waitlist.json')
        self.assertEqual(self.result('候补递补资格'),'候补待空位')
        self.assertEqual(self.result('离港放行裁定'),'放行')
    def test_cancel_after_unboarded_scan_creates_refund(self):
        self.add('stow','stow-bad.json')
        self.add('voyage','voyage-cancel.json')
        self.assertEqual(self.result('停航退票责任'),'应退票')
        self.assertEqual(self.result('离港放行裁定'),'阻断')
    def test_conflicting_tide_windows_freeze_voyage(self):
        self.add('voyage','voyage-bad.json')
        self.assertEqual(self.result('潮窗冲突冻结'),'冻结')
        self.assertEqual(self.result('航次潮窗核定'),'停航')
    def test_lane_specific_weight_rejects_even_when_total_capacity_remains(self):
        self.add('vehicle','vehicle-heavy.json')
        self.assertEqual(self.result('舱道分区额度'),'分区超载')
        self.assertEqual(self.result('舱位配载确认'),'待改配')
    def test_vin_mismatch_rejects_vehicle(self):
        self.add('vehicle','vehicle-scan-mismatch.json')
        self.assertEqual(self.result('车辆身份复核:VIN1'),'拒载')
        self.assertEqual(self.result('离港放行裁定'),'阻断')
    def test_expired_calibration_invalidates_measured_weight(self):
        self.add('vehicle','vehicle-calibration-expired.json')
        self.assertEqual(self.result('实测重量裁定:VIN1'),'待复称')
        self.assertEqual(self.result('离港放行裁定'),'阻断')
    def test_duplicate_lane_blocks_stow_plan(self):
        self.add('stow','stow-conflict.json')
        self.assertEqual(self.result('舱位配载确认'),'待改配')
        self.assertEqual(self.result('离港放行裁定'),'阻断')
