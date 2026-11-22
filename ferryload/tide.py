from datetime import datetime

def _instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))

def evaluate(v):
    departure = _instant(v['departure_at'])
    windows = v.get('tide_windows', [])
    current = [w for w in windows if _instant(w['start']) <= departure < _instant(w['end']) and w['level_cm'] >= v['required_level_cm']]
    conflict = any(_instant(a['start']) < _instant(b['end']) and _instant(b['start']) < _instant(a['end']) and a['level_cm'] != b['level_cm'] for i, a in enumerate(windows) for b in windows[i+1:])
    return bool(current) and not conflict, conflict
