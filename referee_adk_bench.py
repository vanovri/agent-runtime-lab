"""Correctness-gated paired benchmark of a synthetic educational agent runtime."""
import argparse
from datetime import datetime, timezone
import gc
import hashlib
import html
import importlib
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
import tracemalloc

from workload import digest, exercise, tools, turn_payload

ROOT=Path(__file__).resolve().parent
MODULES=('adk_runtime_legacy','adk_runtime_optimized')


def summary(values):
    ordered=sorted(values)
    return {'samples':values,'median':statistics.median(values),'min':min(values),'max':max(values),
            'p10':ordered[int((len(values)-1)*.1)],'p90':ordered[int((len(values)-1)*.9)]}


def cold(module):
    script=("import time,importlib,json; t=time.perf_counter_ns(); m=importlib.import_module("+repr(module)+
            "); a=time.perf_counter_ns(); r=m.GLOBAL_VALIDATOR_REGISTRY; b=time.perf_counter_ns(); "
            "assert len(r)==400; print(json.dumps([(a-t)/1e6,(b-a)/1e6,(b-t)/1e6]))")
    result=subprocess.run([sys.executable,'-c',script],cwd=ROOT,capture_output=True,text=True,check=True,timeout=30)
    return json.loads(result.stdout)


def session_measure(module, turns, nested, expected):
    cls=getattr(module,'OptimizedAgentSession',None) or module.LegacyAgentSession
    session=cls('synthetic-01',tools(module.ToolDefinition))
    enabled=gc.isenabled();gc.disable()
    total_ns=0
    try:
        for turn in range(turns):
            session.append_turn(*turn_payload(turn,nested))
            start=time.perf_counter_ns();result=session.prepare_session_payload()
            total_ns+=time.perf_counter_ns()-start
    finally:
        if enabled: gc.enable()
    if digest(result)!=expected: raise AssertionError('Timed output differs from reference')
    return total_ns/1e6


def peak_memory(module, turns, nested, expected):
    gc.collect();tracemalloc.start()
    try:
        value=exercise(module,turns,nested)
        _,peak=tracemalloc.get_traced_memory()
    finally: tracemalloc.stop()
    if digest(value)!=expected: raise AssertionError('Memory-run output differs')
    return peak


def benchmark(repeats):
    gate=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-v'],cwd=ROOT,
                        capture_output=True,text=True,timeout=90)
    if gate.returncode: raise AssertionError(gate.stdout+gate.stderr)
    modules=[importlib.import_module(name) for name in MODULES]
    imports={name:[] for name in MODULES}
    # First process warms the bytecode/filesystem paths for both variants, excluded.
    for name in MODULES: cold(name)
    for repeat in range(repeats):
        for name in MODULES[::1 if repeat%2==0 else -1]: imports[name].append(cold(name))
    import_report={name:{key:summary([v[i] for v in imports[name]])
                        for i,key in enumerate(('import_ms','first_registry_access_ms','import_plus_registry_ms'))}
                   for name in MODULES}
    cases=[]
    for turns in (50,150):
        for nested in (False,True):
            expected=digest(exercise(modules[0],turns,nested))
            if digest(exercise(modules[1],turns,nested))!=expected: raise AssertionError('Pre-timing parity failed')
            # Warm both variants before retaining measurements.
            for module in modules: session_measure(module,turns,nested,expected)
            samples={name:[] for name in MODULES}
            for repeat in range(repeats):
                for index in ((0,1) if repeat%2==0 else (1,0)):
                    samples[MODULES[index]].append(session_measure(modules[index],turns,nested,expected))
            measurements={name:summary(values) for name,values in samples.items()}
            saving=100*(1-measurements[MODULES[1]]['median']/measurements[MODULES[0]]['median'])
            cases.append({'turns':turns,'nested':nested,'payload_sha256':expected,'prepare_total_ms':measurements,
                          'median_reduction_percent':saving,
                          'whole_session_peak_bytes':{name:peak_memory(module,turns,nested,expected)
                                                      for name,module in zip(MODULES,modules)}})
    import_reduction=100*(1-import_report[MODULES[1]]['import_ms']['median']/import_report[MODULES[0]]['import_ms']['median'])
    return {'schema_version':1,'status':'pass','data':'synthetic','generated_utc':datetime.now(timezone.utc).isoformat(),
            'environment':{'python':sys.version,'platform':platform.platform(),'timer':'perf_counter_ns'},
            'method':{'repeats':repeats,'order':'alternating pairs','warmups':1,'gc_during_timing':'disabled/restored',
                      'timed_region':'sum of prepare_session_payload calls, append/construction excluded',
                      'memory':'separate tracemalloc whole-session run, not used for timing',
                      'import':'fresh processes, module span excludes interpreter startup; first registry access shown separately'},
            'unit_test_log':gate.stdout+gate.stderr,'sources':{p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                for p in [ROOT/'adk_runtime_legacy.py',ROOT/'adk_runtime_optimized.py',Path(__file__),ROOT/'workload.py']},
            'imports':import_report,'import_median_reduction_percent':import_reduction,'workloads':cases,
            'requested_targets':{'import_reduction_percent':20,'prepare_reduction_percent':40},
            'performance_targets_met':import_reduction>=20 and all(c['median_reduction_percent']>=40 for c in cases)}


def render(report):
    rows=[]
    for case in report['workloads']:
        a,b=(case['prepare_total_ms'][name]['median'] for name in MODULES)
        rows.append(f"<tr><td>{case['turns']} / {'nested' if case['nested'] else 'scalar'}</td><td>{a:.3f}</td><td>{b:.3f}</td><td>{case['median_reduction_percent']:.1f}%</td></tr>")
    imports=[]
    for name in MODULES:
        values=report['imports'][name]
        imports.append('<tr><td>'+html.escape(name)+'</td>'+''.join(f"<td>{values[key]['median']:.3f}</td>" for key in ('import_ms','first_registry_access_ms','import_plus_registry_ms'))+'</tr>')
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Agent Runtime Lab | Measured report</title><style>
body{margin:0;background:#f4f3ef;color:#24322c;font:16px/1.6 system-ui,sans-serif}main{max-width:1040px;margin:auto;padding:56px 24px}h1{font-size:clamp(32px,5vw,58px);line-height:1.08;letter-spacing:-2px;max-width:760px}h2{font-size:25px;margin-top:45px}small{font:12px monospace}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;background:#fff}th,td{text-align:left;padding:14px;border-bottom:1px solid #d6ded8}th{background:#e5ebe4}p{max-width:850px}.table{overflow:auto}a{color:inherit}.badge{display:inline-block;padding:6px 12px;background:#dce8dc;border-radius:4px}details{margin-top:30px;background:#fff;padding:18px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}</style><main><small>SYNTHETIC / REPRODUCIBLE / PYTHON</small><h1>Faster session preparation.<br>Keep the mutation contract.</h1><span class="badge">Correctness gates passed</span><p>This measured educational runtime preserves nested input snapshots, fresh output containers, mutable public events and tool changes. No artificial sleeps. This is not an upstream Google ADK benchmark.</p><h2>Preparation time</h2><p>Median total milliseconds spent in prepare calls. Construction and append are excluded. Alternating paired repeats; raw samples remain in JSON.</p><div class="table"><table><tr><th>Turns / payload</th><th>Reference ms</th><th>Candidate ms</th><th>Reduction</th></tr>'''+''.join(rows)+'''</table></div><h2>Cold import and deferred work</h2><p>Fresh Python processes. A lazy import defers validator compilation rather than removing its cost. Both the first-use cost and total are shown.</p><div class="table"><table><tr><th>Module</th><th>Import ms</th><th>First access ms</th><th>Combined ms</th></tr>'''+''.join(imports)+'''</table></div><p>Full-history payloads still require O(history length) work. The candidate folds current public history and copies the final merged state instead of copying every superseded event delta. Schema digest caching invalidates on nested parameter changes. Results depend on workload and machine.</p><details><summary>Method, source hashes and exact measurements</summary><pre>'''+html.escape(json.dumps(report,indent=2))+'''</pre></details><p><a href="https://github.com/vanovri/agent-runtime-lab">Source and contract tests</a> · Developed with Codex assistance.</p></main></html>'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats',type=int,default=9)
    parser.add_argument('--report',type=Path,default=ROOT/'reports'/'local.json')
    parser.add_argument('--html',type=Path)
    parser.add_argument('--enforce-targets',action='store_true')
    args=parser.parse_args()
    if not 3<=args.repeats<=31: parser.error('repeats must be 3..31')
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text('{"status":"running"}\n')
    if args.html:
        args.html.parent.mkdir(parents=True,exist_ok=True)
        args.html.write_text('<!doctype html><title>Benchmark running</title><p>Benchmark running. No current result yet.</p>',encoding='utf-8')
    try:
        report=benchmark(args.repeats)
        if args.enforce_targets and not report['performance_targets_met']:
            report['status']='targets_not_met'
        args.report.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        if args.html:
            args.html.parent.mkdir(parents=True,exist_ok=True);args.html.write_text(render(report),encoding='utf-8')
        print(json.dumps({'status':report['status'],'import_reduction_percent':report['import_median_reduction_percent'],
                          'prepare_reduction_percent':[c['median_reduction_percent'] for c in report['workloads']],
                          'targets_met':report['performance_targets_met']}))
        return 0 if report['status']=='pass' else 1
    except Exception as error:
        args.report.write_text(json.dumps({'status':'failed','error':str(error)},indent=2)+'\n')
        if args.html:
            args.html.write_text('<!doctype html><title>Benchmark failed</title><h1>Benchmark failed</h1><pre>'+html.escape(str(error))+'</pre>',encoding='utf-8')
        raise


if __name__=='__main__': raise SystemExit(main())
