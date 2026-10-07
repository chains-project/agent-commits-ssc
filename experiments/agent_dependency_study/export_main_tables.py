"""Export the four rebuilt Stage1/2 tables faithfully as compressed CSV files."""
import argparse
import csv
import gzip
import io
import json
import sys
from pathlib import Path

BASE=Path(__file__).resolve().parent
sys.path[:0]=[str(BASE/'src'),str(BASE/'scripts/hallucination')]
from common import read_db,unpack,digest,write_json
from stage_schema import TABLE_CONTRACTS

TABLES=[('commits','commits.csv','id'),('events','events.csv','cid'),
        ('episodes','episodes.csv','id'),('links','episode_event_links.csv','cid')]


def source_rows(db,table,key):
    for saved in db.execute('SELECT body FROM '+table+' ORDER BY '+key):
        body=unpack(saved['body'])
        yield from body if isinstance(body,list) else [body]


def export_one(db,table,name,key,output):
    fields=TABLE_CONTRACTS[name].fields;path=output/(name+'.gz');count=0
    with path.open('wb') as raw:
        with gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0,compresslevel=1) as packed:
            with io.TextIOWrapper(packed,encoding='utf-8',newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader()
                for row in source_rows(db,table,key):
                    assert set(row)==set(fields),(name,set(row)^set(fields))
                    writer.writerow(row);count+=1
    print(name,count,'rows exported',flush=True)
    return dict(path=path.name,rows=count,bytes=path.stat().st_size,sha256=digest(path),fields=list(fields))


def verify_csv(output,record):
    with gzip.open(output/record['path'],'rt',encoding='utf-8',newline='') as handle:
        reader=csv.reader(handle);assert next(reader)==record['fields'];count=0
        for row in reader:
            assert len(row)==len(record['fields']),record['path'];count+=1
    assert count==record['rows'],record['path']
    return count


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage12',type=Path,default=BASE/'results_replay_v2')
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    summary=json.loads((args.stage12/'stage12_summary.json').read_text())
    source_hash=digest(args.stage12/'stage12.sqlite');assert source_hash==summary['output_sha256']
    db=read_db(args.stage12/'stage12.sqlite');records=[]
    for table,name,key in TABLES:
        record=export_one(db,table,name,key,args.output)
        assert record['rows']==summary['counts'][table]
        verify_csv(args.output,record);records.append(record)
    db.close()
    manifest=dict(source_stage12_sha256=source_hash,tables=records,verification='All CSV headers, row widths, row counts and gzip streams passed; source DB hash matches the verified integrated run.')
    write_json(args.output/'manifest.json',manifest)
    print(json.dumps({r['path']:r['rows'] for r in records}),flush=True)


if __name__=='__main__':main()
