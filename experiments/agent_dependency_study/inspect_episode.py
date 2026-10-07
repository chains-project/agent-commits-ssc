"""Export a single episode, applicable observations and new decisions for debugging."""
import argparse
import json
import sys
from pathlib import Path
BASE=Path(__file__).resolve().parent
sys.path.insert(0,str(BASE/'src'))
from common import read_db,unpack
from stage3_classify import classify
from stage3_matching import Matcher


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--episode',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--node',default='node')
    args=parser.parse_args()
    db=read_db(BASE/'inputs/observations.sqlite')
    row=db.execute('SELECT * FROM episodes WHERE id=?',(args.episode,)).fetchone()
    if row is None:
        raise SystemExit('Episode ID not found')
    episode=unpack(row['body'])
    auxiliary=db.execute('SELECT body FROM auxiliary WHERE eid=?',(args.episode,)).fetchone()
    aux=unpack(auxiliary['body']) if auxiliary else {}
    target=aux.get('effective_package',row['pkg'])
    records=[(r['id'],unpack(r['body'])) for r in db.execute('SELECT id,body FROM observations WHERE eco=? AND pkg=? ORDER BY id',(row['eco'],target))]
    commit=dict(db.execute('SELECT * FROM commits WHERE id=?',(row['cid'],)).fetchone())
    boundary=commit['committer'] or commit['author']
    matcher=Matcher(args.node)
    decision=classify(episode,records,boundary,matcher,aux)
    sensitivity=classify(episode,records,boundary,matcher,aux,exclude_proxies=True)
    matcher.close()
    old=db.execute('SELECT label FROM old_labels WHERE eid=?',(args.episode,)).fetchone()
    result=dict(episode=episode,baseline_queryable=row['q'],commit=commit,auxiliary=aux,
                observations=[dict(evidence_id=key,record=value) for key,value in records],
                new_decision=decision,without_proxy_times=sensitivity,
                old_label_for_comparison_only=old['label'] if old else None)
    with args.output.open('x',encoding='utf-8') as handle:
        json.dump(result,handle,ensure_ascii=False,indent=2)
    print(json.dumps(decision,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
