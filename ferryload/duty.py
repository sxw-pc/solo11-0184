from datetime import datetime

def worked_minutes(entries):
    total = 0
    for entry in entries:
        start = datetime.fromisoformat(entry['start'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(entry['end'].replace('Z', '+00:00'))
        total += int((end-start).total_seconds()//60)
    return total
