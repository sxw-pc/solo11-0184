import argparse
import json
from pathlib import Path
from .store import connect,import_file,report,audit

IMPORTS={'voyage-import': 'voyage', 'vehicle-import': 'vehicle', 'stow-import': 'stow'}
def main(argv=None):
    parser=argparse.ArgumentParser(prog='ferryload')
    parser.add_argument('command',choices=list(IMPORTS)+['demo','report','audit'])
    parser.add_argument('--db',default='state.sqlite')
    parser.add_argument('--file')
    args=parser.parse_args(argv)
    db=connect(args.db)
    if args.command in IMPORTS:
        if not args.file: parser.error('--file required')
        result=import_file(db,IMPORTS[args.command],args.file)
    elif args.command=='demo':
        root=Path(__file__).resolve().parents[1]/'fixtures'
        result=[]
        for command,file in DEMO:
            result.append(import_file(db,IMPORTS[command],root/file))
        result={'imports':result,'report':report(db)}
    elif args.command=='report': result=report(db)
    else: result=audit(db)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if args.command!='audit' or result['ok'] else 1

DEMO=[('voyage-import', 'voyage-ok.json'), ('vehicle-import', 'vehicle-ok.json'), ('stow-import', 'stow-ok.json')]
