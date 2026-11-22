import json
from pathlib import Path
from ferryload import seal, custody

def test_seal_to_custody_business_flow():
    record = json.loads((Path(__file__).resolve().parents[1]/'fixtures'/'cargo-seal-ok.json').read_text(encoding='utf-8'))
    assert seal.unchanged(record)
    assert custody.assess(record) == {'cargo_id': 'CARGO-1', 'result': '封签一致'}
