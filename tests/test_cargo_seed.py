import json
from pathlib import Path
from ferryload import cargo, quality

def test_cold_cargo_business_flow():
    record = json.loads((Path(__file__).resolve().parents[1]/'fixtures'/'cargo-temperature-ok.json').read_text(encoding='utf-8'))
    assert cargo.maximum_temperature_c(record) == 4
    assert quality.assess(record) == {'result': '合格', 'maximum_c': 4}
