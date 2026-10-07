"""Verify high-frequency Java import owners against official source-tree metadata.

Explicit online research step. Saves public metadata, source references and hashes;
the experiment replay remains offline. Does not fetch registry release histories.
"""
import argparse
import csv
import hashlib
import json
import re
import time
import urllib.request
from pathlib import Path
BASE=Path(__file__).resolve().parent

PROJECTS = [
 ('spring-projects/spring-framework','v6.2.5','org.springframework',{'spring-context':'spring-context','spring-web':'spring-web','spring-beans':'spring-beans','spring-tx':'spring-tx','spring-test':'spring-test','spring-core':'spring-core'}),
 ('spring-projects/spring-boot','v3.4.4','org.springframework.boot',{'spring-boot-project/spring-boot':'spring-boot','spring-boot-project/spring-boot-autoconfigure':'spring-boot-autoconfigure','spring-boot-project/spring-boot-test':'spring-boot-test'}),
 ('spring-projects/spring-data-jpa','3.4.4','org.springframework.data',{'spring-data-jpa':'spring-data-jpa'}),
 ('spring-projects/spring-data-commons','3.4.4','org.springframework.data',{'':'spring-data-commons'}),
 ('spring-projects/spring-security','6.4.4','org.springframework.security',{'core':'spring-security-core','crypto':'spring-security-crypto','config':'spring-security-config','web':'spring-security-web'}),
 ('FasterXML/jackson-databind','jackson-databind-2.18.3','com.fasterxml.jackson.core',{'':'jackson-databind'}),
 ('FasterXML/jackson-annotations','jackson-annotations-2.18.3','com.fasterxml.jackson.core',{'':'jackson-annotations'}),
 ('mockito/mockito','v5.15.2','org.mockito',{'mockito-core':'mockito-core','':'mockito-core','mockito-extensions/mockito-junit-jupiter':'mockito-junit-jupiter'}),
 ('assertj/assertj','assertj-build-3.27.3','org.assertj',{'assertj-core':'assertj-core'}),
 ('projectlombok/lombok','v1.18.36','org.projectlombok',{'src/core':'lombok'}),
 ('junit-team/junit5','r5.12.0','org.junit.jupiter',{'junit-jupiter-api':'junit-jupiter-api'}),
 ('junit-team/junit4','r4.13.2','junit',{'':'junit'}),
 ('jakartaee/validation','3.1.1','jakarta.validation',{'':'jakarta.validation-api'}),
 ('jakartaee/persistence','3.2-3.2.0-RELEASE','jakarta.persistence',{'api':'jakarta.persistence-api'}),
 ('jakartaee/servlet','6.1.0-RELEASE','jakarta.servlet',{'api':'jakarta.servlet-api'}),
 ('jwtk/jjwt','0.12.6','io.jsonwebtoken',{'api':'jjwt-api'}),
]


def fetch(url, path):
    if path.exists():
        return path.read_bytes()
    request=urllib.request.Request(url,headers={'User-Agent':'dependency-study-identity-research','Accept':'application/vnd.github+json'})
    with urllib.request.urlopen(request,timeout=30) as response:
        body=response.read()
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(body)
    return body


def source_index(tree,group,modules):
    index={}
    for row in tree['tree']:
        path=row['path']
        if row['type']!='blob' or not path.endswith('.java'):
            continue
        for module,artifact in modules.items():
            prefix=(module+'/' if module else '')+'src/main/java/'
            if module=='src/core':
                prefix='src/core/'
            if path.startswith(prefix):
                name=path[len(prefix):-5].replace('/','.')
                index[name]=dict(coordinate=group+':'+artifact,path=path,blob_sha=row['sha'])
    return index


def match_target(target,index):
    if target in index:
        return [index[target]],'class'
    parent,_,member=target.rpartition('.')
    if parent in index:
        return [index[parent]],'class_members' if member=='*' else 'static_member_owner'
    if member=='*':
        rows=[r for name,r in index.items() if name.rpartition('.')[0]==parent and not name.endswith('package-info')]
        if rows and len({r['coordinate'] for r in rows})==1:
            return rows,'package_members'
    return [],''


def inspect_project(config,targets,output):
    repo,ref,group,modules=config
    tree_url=f'https://api.github.com/repos/{repo}/git/trees/{ref}?recursive=1'
    cache=output/'trees'/(repo.replace('/','__')+'.json')
    tree=json.loads(fetch(tree_url,cache))
    if tree.get('truncated'):
        raise ValueError('Truncated tree: '+repo)
    index=source_index(tree,group,modules)
    matches=[]
    for target in targets:
        rows,kind=match_target(target,index)
        if not rows:
            continue
        source=rows[0]
        proof=dict(repo=repo,requested_ref=ref,tree_sha=tree['sha'],tree_url=tree_url,
            tree_sha256=hashlib.sha256(cache.read_bytes()).hexdigest(),match_kind=kind,source_files=rows)
        if kind=='static_member_owner':
            url=f'https://raw.githubusercontent.com/{repo}/{ref}/{source["path"]}'
            code=fetch(url,output/'members'/(hashlib.sha256(url.encode()).hexdigest()+'.java')).decode('utf-8')
            member=target.rsplit('.',1)[1]
            if not re.search(r'\b'+re.escape(member)+r'\s*\(',code):
                continue
            proof['member_source_url']=url
            proof['member_source_sha256']=hashlib.sha256(code.encode()).hexdigest()
        matches.append(dict(target=target,coordinate=source['coordinate'],proof=proof,
            sources=[f'https://github.com/{repo}/blob/{ref}/{r["path"]}' for r in rows]))
    return matches


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    old=json.loads((BASE/'scripts/hallucination/java_class_catalog.json').read_text())
    existing={t for r in old['rules'] for t in r['targets']}
    rows=list(csv.DictReader((BASE/'results_identity_audit/java_top_unresolved_targets.csv').open()))
    targets=[r['target'] for r in rows if r['target'] not in existing]
    matches,errors=[],[]
    for config in PROJECTS:
        try:
            found=inspect_project(config,targets,args.output)
            matches.extend(found)
            print(config[0],len(found),'targets',flush=True)
        except Exception as error:
            errors.append(dict(repo=config[0],error=str(error)))
            print(config[0],str(error),flush=True)
        time.sleep(0.2)
    (args.output/'candidate_mappings.json').write_text(json.dumps(dict(matches=matches,errors=errors),indent=2),encoding='utf-8')
    matched={r['target'] for r in matches}
    print('Targets verified:',len(matched),'Uncovered:',[t for t in targets if t not in matched],flush=True)


if __name__=='__main__':
    main()
