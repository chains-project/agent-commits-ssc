"""Bind saved child-file structure to real diff-added positions, offline.

[IN]: FilePatch and the exact child manifest text for its repo/SHA/path.
[OUT]: Re-extracted manifest observations, or an explicit mismatch/parse status.
[SYNC]: stage1_manifest_diff, integrated replay and snapshot regression tests.
"""
import json
import re
from dataclasses import replace
from stage1_manifest_diff import (FilePatch,PatchLine,manifest_descriptor,
    extract_manifest_additions,_manifest_observation,NPM_DEPENDENCY_SECTIONS,is_research_file_path)


def replay_manifest(patch,text):
    if not is_research_file_path(patch.path):return None,'non_research_path'
    lines=text.splitlines()
    for row in patch.lines:
        if row.new_line_number<1 or row.new_line_number>len(lines):
            return None,'snapshot_line_mismatch'
        if lines[row.new_line_number-1]!=row.text:
            return None,'snapshot_text_mismatch'
    added={r.new_line_number for r in patch.added_lines}
    if patch.path.rsplit('/',1)[-1]=='pom.xml':
        masked=re.sub(r'<!--.*?-->',lambda m:re.sub(r'[^\r\n]',' ',m.group()),text,flags=re.S)
        lines=masked.splitlines()
    full=FilePatch(patch.path,tuple(PatchLine(i,0,t,i in added) for i,t in enumerate(lines,1)))
    descriptor=manifest_descriptor(patch.path)
    try:
        if descriptor.parser=='npm_json':
            events=json_events(full,text,added,descriptor)
        else:
            if descriptor.parser=='requirements':full=logical_requirements(full)
            events=extract_manifest_additions(full)
    except (ValueError,TypeError,KeyError):
        return None,'snapshot_parse_failed'
    return [replace(r,reason_codes=tuple(sorted(set(r.reason_codes)|{'child_snapshot_structure'}))) for r in events],'snapshot_applied'


def logical_requirements(patch):
    result=[];pending=[]
    for line in patch.lines:
        pending.append(line)
        if line.text.rstrip().endswith('\\'):
            continue
        if len(pending)==1:
            result.append(line)
        else:
            text=' '.join(r.text.rstrip().rstrip('\\').strip() for r in pending)
            text=re.sub(r'\s+--hash(?:=|\s+)\S+','',text)
            anchors=[r for r in pending if r.is_added and requirement_component(r.text)]
            anchor=anchors[0] if anchors else pending[0]
            result.append(PatchLine(anchor.new_line_number,0,text,bool(anchors)))
        pending=[]
    if pending:raise ValueError('Incomplete requirement continuation')
    return FilePatch(patch.path,tuple(result))


def requirement_component(text):
    value=re.sub(r'--hash(?:=|\s+)\S+','',text).strip().rstrip('\\').strip()
    return bool(value and not value.startswith('#'))


def object_items(text,start):
    decoder=json.JSONDecoder()
    pos=start+1
    while True:
        pos=skip_space(text,pos)
        if text[pos]=='}':return
        key_start=pos
        key,pos=decoder.raw_decode(text,pos)
        pos=skip_space(text,pos)
        if text[pos]!=':':raise ValueError('Missing colon')
        value_start=skip_space(text,pos+1)
        value,end=decoder.raw_decode(text,value_start)
        yield key,value,key_start,value_start,end
        pos=skip_space(text,end)
        if text[pos]=='}':return
        if text[pos]!=',':raise ValueError('Missing comma')
        pos+=1


def skip_space(text,pos):
    while pos<len(text) and text[pos].isspace():pos+=1
    return pos


def json_events(patch,text,added,descriptor):
    if not isinstance(json.loads(text),dict):raise ValueError('Expected object')
    events=[];ordinals={}
    for section,value,_,start,_end in object_items(text,skip_space(text,0)):
        if section not in NPM_DEPENDENCY_SECTIONS or not isinstance(value,dict):continue
        for key,version,left,_right,end in object_items(text,start):
            if not isinstance(version,str):continue
            first=text.count('\n',0,left)+1;last=text.count('\n',0,end)+1
            anchors=sorted(added.intersection(range(first,last+1)))
            if not anchors:continue
            anchor=anchors[0];ordinal=ordinals.get(anchor,0);ordinals[anchor]=ordinal+1
            line=PatchLine(anchor,0,patch.lines[anchor-1].text,True)
            events.append(_manifest_observation(patch,descriptor,line,ordinal,key,version))
    return events
