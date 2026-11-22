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
