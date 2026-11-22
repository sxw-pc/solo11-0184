import json
import tempfile
import unittest
from pathlib import Path
import ferryload
from ferryload.store import connect,import_file,report,audit

ROOT=Path(__file__).resolve().parents[1]
class BusinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=connect(Path(self.tmp.name)/'state.sqlite')
    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()
    def load(self,command,file): return import_file(self.db,IMPORTS[command],ROOT/'fixtures'/file)
    def test_normal_decisions_and_replay(self):
        for cmd,file in DEMO: self.load(cmd,file)
        actual=report(self.db)
        self.assertGreaterEqual(len(actual['decisions']),10)
        self.assertTrue(audit(self.db)['ok'])
        self.assertTrue(all(d['evidence'] for d in actual['decisions'].values()))
        print('module file', ferryload.__file__)
        self.assertTrue(str(Path(ferryload.__file__).resolve()).startswith(str(ROOT)))
    def test_idempotency_and_conflict_rollback(self):
        cmd,file=DEMO[0]
        first=self.load(cmd,file)
        self.assertTrue(self.load(cmd,file)['idempotent'])
        original=report(self.db)
        data=json.loads((ROOT/'fixtures'/file).read_text(encoding='utf-8'))
        data[0]['value']='changed'
        conflict=Path(self.tmp.name)/'conflict.json'
        conflict.write_text(json.dumps(data),encoding='utf-8')
        with self.assertRaises(ValueError): import_file(self.db,IMPORTS[cmd],conflict)
        self.assertEqual(original,report(self.db))
    def test_boundaries_change_outcomes(self):
        for cmd,file in DEMO: self.load(cmd,file)
        before=report(self.db)['decisions']
        for cmd,file in BAD: self.load(cmd,file)
        after=report(self.db)['decisions']
        changed=[name for name in before if before[name]['result']!=after[name]['result']]
        self.assertGreaterEqual(len(changed),3,changed)
        self.assertTrue(audit(self.db)['ok'])
    def test_six_distinct_business_entities(self):
        for cmd,file in DEMO: self.load(cmd,file)
        records=report(self.db)['business_records']
        self.assertGreaterEqual(len(set((r['subsystem'],r['entity_type']) for r in records)),6)
        self.assertTrue(all(r['source_event'] for r in records))
    def test_audit_detects_materialized_state_tampering(self):
        for cmd,file in DEMO: self.load(cmd,file)
        name=next(iter(report(self.db)['decisions']))
        self.db.execute('UPDATE decisions SET result=? WHERE name=?',('FORGED',name))
        self.db.commit()
        self.assertFalse(audit(self.db)['ok'])
    def test_expected_normal_conclusions(self):
        for cmd,file in DEMO: self.load(cmd,file)
        decisions=report(self.db)['decisions']
        for name,expected in NORMAL.items():
            with self.subTest(name=name): self.assertEqual(decisions[name]['result'],expected)
    def test_each_subsystem_changes_own_decision(self):
        for cmd,file,decision in CROSS:
            with self.subTest(decision=decision):
                sample=connect(':memory:')
                for normal_cmd,normal_file in DEMO:
                    import_file(sample,IMPORTS[normal_cmd],ROOT/'fixtures'/normal_file)
                before=report(sample)['decisions'][decision]['result']
                import_file(sample,IMPORTS[cmd],ROOT/'fixtures'/file)
                after=report(sample)['decisions'][decision]['result']
                self.assertNotEqual(before,after)
                sample.close()

IMPORTS={'voyage-import': 'voyage', 'vehicle-import': 'vehicle', 'stow-import': 'stow'}
DEMO=[('voyage-import', 'voyage-ok.json'), ('vehicle-import', 'vehicle-ok.json'), ('stow-import', 'stow-ok.json')]
BAD=[('voyage-import', 'voyage-bad.json'), ('vehicle-import', 'vehicle-bad.json'), ('stow-import', 'stow-bad.json')]
NORMAL={'航次潮窗核定': '开放', '舱道分区额度': '有余量', '车辆身份复核:VIN1': '通过', '实测重量裁定:VIN1': '有效', '危险品隔离核验:VIN1': '合格', '舱位配载确认': '确认', '登船与离船闭环': '闭环', '离港放行裁定': '放行'}
CROSS=[('voyage-import', 'voyage-bad.json', '离港放行裁定'), ('vehicle-import', 'vehicle-bad.json', '离港放行裁定'), ('stow-import', 'stow-bad.json', '离港放行裁定')]
