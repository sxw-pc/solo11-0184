import json
import sys
from pathlib import Path
from . import cargo

def assess(record):
    maximum = cargo.maximum_temperature_c(record)
    if maximum is None:
        return {'result': '待测', 'maximum_c': None}
    return {
        'result': '合格' if maximum <= record['allowed_max_c'] else '超限',
        'maximum_c': maximum,
    }

if __name__ == '__main__':
    print(json.dumps(assess(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))), ensure_ascii=False))
