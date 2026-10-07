"""Normalize unioned I/M scope for an in-progress export started before that fix."""
import json
import sqlite3
import sys
from pathlib import Path
BASE=Path(__file__).resolve().parent
sys.path.insert(0,str(BASE/'src'))
from common import csv_rows,pack,unpack,digest,write_json


def main():
    target=BASE/'inputs/observations.sqlite'
    db=sqlite3.connect(target)
    masks={}
    for (value,) in db.execute('SELECT body FROM sources'):
        path=Path(unpack(value)['path'])
        if path.name!='episodes.csv':
            continue
        for row in csv_rows(path):
            quad=row['dependency_quadrant']
            mask=int('I+' in quad)+2*int('M+' in quad)
            masks[row['episode_id']]=masks.get(row['episode_id'],0)|mask
    changes=0
    for eid,body in db.execute('SELECT id,body FROM episodes'):
        row=unpack(body)
        quad=('I+' if masks[eid]&1 else 'I-')+('M+' if masks[eid]&2 else 'M-')
        if row['dependency_quadrant']!=quad:
            row['dependency_quadrant']=quad
            db.execute('UPDATE episodes SET body=? WHERE id=?',(pack(row),eid))
            changes+=1
    db.commit()
    db.close()
    path=BASE/'inputs/manifest.json'
    manifest=json.loads(path.read_text(encoding='utf-8'))
    manifest.update(input_sha256=digest(target),scope_union_corrections=changes)
    write_json(path,manifest)
    print('Scope union corrections:',changes)


if __name__=='__main__':
    main()
