"""Summarize saved Stage2 query kinds without rerunning classification.

Input: current Stage1/2 database and integrated summary. Output: new CSV and
source-hash receipt. Read-only aggregation; no registry or attribution queries.
"""
import argparse,csv,hashlib,json,sqlite3
from pathlib import Path
BASE=Path(__file__).resolve().parent


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda:handle.read(4*1024*1024),b''):h.update(block)
    return h.hexdigest()


def summarize(database,summary_path,output):
    summary=json.loads(summary_path.read_text(encoding='utf-8'))
    fingerprint=digest(database)
    assert fingerprint==summary['stage12']['output_sha256']
    db=sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)
    sql="SELECT eco,kind,q,count(*) FROM episodes GROUP BY eco,kind,q UNION ALL SELECT 'global',kind,q,count(*) FROM episodes GROUP BY kind,q"
    rows=sorted(db.execute(sql));db.close()
    global_rows=[r for r in rows if r[0]=='global']
    assert sum(r[3] for r in global_rows)==summary['episodes']
    assert sum(r[3] for r in global_rows if r[2])==summary['queryable']
    for _,kind,q,n in global_rows:
        assert sum(r[3] for r in rows if r[0]!='global' and r[1:3]==(kind,q))==n
    output.mkdir(parents=True,exist_ok=False)
    with (output/'query_kind_counts.csv').open('w',encoding='utf-8',newline='') as handle:
        writer=csv.writer(handle);writer.writerow(['ecosystem','query_kind','queryable','episodes']);writer.writerows(rows)
    receipt=dict(dataset='integrated_20261007',source_stage12_sha256=fingerprint,summary_sha256=digest(summary_path),code_sha256=digest(Path(__file__)),output_sha256=digest(output/'query_kind_counts.csv'),episodes=summary['episodes'],queryable=summary['queryable'],validation='Global count and queryable closure; ecosystem partition by query kind.')
    (output/'PROVENANCE.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(global_query_kinds={r[1]:r[3] for r in global_rows},status='passed')))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database',type=Path,default=BASE/'results_replay_v2/stage12.sqlite')
    p.add_argument('--summary',type=Path,default=BASE/'results_integrated/summary.json')
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();summarize(args.database,args.summary,args.output)
