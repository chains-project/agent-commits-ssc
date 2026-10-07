"""Build compact local inputs from existing frozen data; no network requests."""
import argparse
import sqlite3
import sys
from pathlib import Path
BASE=Path(__file__).resolve().parent
sys.path.insert(0,str(BASE/'src'))
from common import digest,unpack,write_json
from stage3_prepare_base import SCHEMA,load_canonical
from stage3_prepare_corrections import load_corrections


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,required=True)
    args=parser.parse_args()
    target=BASE/'inputs/observations.sqlite'
    target.parent.mkdir(exist_ok=True)
    if target.exists():
        raise FileExistsError(target)
    db=sqlite3.connect(target)
    db.execute('PRAGMA journal_mode=OFF')
    db.execute('PRAGMA synchronous=OFF')
    db.executescript(SCHEMA)
    canonical=args.project/'data_products/rq2_phase9_targeted_pep508_six_language_20260816_v2'
    load_canonical(db,canonical)
    load_corrections(db,canonical,args.project)
    counts={name:db.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in ['episodes','commits','observations','auxiliary','raw','old_labels']}
    assert counts['episodes']==1769653
    assert db.execute('SELECT sum(q) FROM episodes').fetchone()[0]==1364856
    sources=[unpack(row[0]) for row in db.execute('SELECT body FROM sources ORDER BY name')]
    db.commit()
    db.close()
    write_json(BASE/'inputs/manifest.json',dict(counts=counts,input_sha256=digest(target),sources=sources))
    print(counts,flush=True)


if __name__=='__main__':
    main()
