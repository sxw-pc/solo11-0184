import json
from pathlib import Path
from ferryload.tide import evaluate

def test_tide_snapshot_baseline():
    voyage=json.loads((Path(__file__).parents[1]/'fixtures'/'voyage-ok.json').read_text())[0]
    assert evaluate(voyage)==(True,False)
