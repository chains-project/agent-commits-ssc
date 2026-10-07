"""Run the integrated local workflow, optionally reusing a completed Stage1/2 DB."""
import argparse
import subprocess
import sys
from pathlib import Path
BASE=Path(__file__).resolve().parent


def invoke(script,*arguments):
    subprocess.run([sys.executable,str(BASE/script),*[str(x) for x in arguments]],check=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--reuse-stage12',type=Path)
    parser.add_argument('--commit-times',type=Path,default=BASE/'inputs/commit_times')
    parser.add_argument('--raw-dir',type=Path,action='append')
    parser.add_argument('--node',default='node')
    parser.add_argument('--update-report',action='store_true')
    args=parser.parse_args()
    if not args.reuse_stage12 and not args.raw_dir:
        parser.error('Full Stage1/2 rebuild requires --raw-dir inputs to rebuild commit dates for that population.')
    args.output.mkdir(parents=True,exist_ok=False)
    stage12=args.reuse_stage12 or args.output/'stage12'
    if not args.reuse_stage12:
        invoke('run_stage12_replay.py','--output',stage12)
        args.commit_times=args.output/'commit_times'
        raw_args=[value for folder in args.raw_dir for value in ('--raw-dir',folder)]
        invoke('prepare_commit_times.py','--stage12',stage12/'stage12.sqlite',
               '--output',args.commit_times,*raw_args)
    invoke('run_stage3_replay.py','--stage12',stage12,'--output',args.output/'results',
           '--node',args.node,'--commit-times',args.commit_times)
    invoke('verify_integrated_results.py','--stage12',stage12,'--results',args.output/'results')
    if args.update_report:invoke('make_report.py','--results',args.output/'results')


if __name__=='__main__':main()
