"""Single deterministic report producer. pytest remains the correctness authority."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / 'reports'
REPORTS.mkdir(exist_ok=True)
junit = REPORTS / 'quant-regression.junit.xml'
junit.unlink(missing_ok=True)
command = [sys.executable, '-m', 'pytest', 'quant/tests', '-q', f'--junitxml={junit}']
run = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
display_command = ['python', '-m', 'pytest', 'quant/tests', '-q', '--junitxml=reports/quant-regression.junit.xml']
(REPORTS / 'quant-regression.log').write_text(run.stdout)
print(run.stdout)
cases = []
if junit.exists():
    for case in ET.parse(junit).iter('testcase'):
        status = 'FAILED' if case.find('failure') is not None or case.find('error') is not None else 'SKIPPED' if case.find('skipped') is not None else 'PASSED'
        cases.append({'id': case.get('classname', '') + '.' + case.get('name', ''), 'status': status, 'seconds': float(case.get('time', 0))})
def group(tokens):
    rows = [c for c in cases if any(t in c['id'] for t in tokens) and not any(optional in c['id'] for optional in ['test_sandbox.', 'test_python_runtime.', 'test_python_jobs.'])]
    status = 'FAILED' if any(c['status'] == 'FAILED' for c in rows) else 'PASSED' if rows and all(c['status'] == 'PASSED' for c in rows) else 'SKIPPED' if rows else 'NOT_TESTED'
    return {'status': status, 'scope': 'Deterministic DSL gates; optional sandbox tests are accounted separately in suite counts.', 'cases': rows}
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
source_files = sorted((ROOT/'quant/src').rglob('*.py')) + sorted((ROOT/'quant/tests').rglob('*.py'))
report = {
    'schema_version': '0.2', 'generated_at': datetime.now(timezone.utc).isoformat(),
    'correctness_status': 'PASSED' if run.returncode == 0 and cases else 'FAILED',
    'golden_oracle_results': group(['oracle', 'golden']),
    'pit_mutation_results': group(['mutation', 'future', 'corrupt']),
    'multi_timeframe_results': group(['alignment', 'calendar', 'fixture', 'multitimeframe']),
    'parameter_stability': group(['parameter', 'research']),
    'walk_forward_results': group(['walk_forward', 'optimization', 'fold']),
    'cost_sensitivity': group(['cost', 'commission', 'slippage']),
    'semantic_grader_results': {'status': 'NOT_TESTED', 'reason': 'Run npm run test:quant-semantic; separate executable evidence is merged by acceptance runner.'},
    'unsupported_capabilities': ['live trading', 'short selling', 'multi-currency accounting', 'Python sandbox unless explicitly configured'],
    'reproducibility': {'command': display_command, 'python': sys.version, 'source_sha256': {str(p.relative_to(ROOT)): sha(p) for p in source_files}},
    'evidence_boundary': 'Grouped pytest cases are executable coverage, not independent certification of every financial assumption. Skips remain explicit in counts and cases.',
    'research_warnings': ['Synthetic inputs do not establish market predictive power.', 'Profitability is not a correctness gate.'],
    'skipped_cases': [c for c in cases if c['status'] == 'SKIPPED'],
    'counts': {s: sum(c['status'] == s for c in cases) for s in ['PASSED', 'FAILED', 'SKIPPED']},
}
research_run = subprocess.run([sys.executable, 'scripts/quant-research-regression.py'], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
(REPORTS/'quant-research.log').write_text(research_run.stdout)
print(research_run.stdout)
if research_run.returncode == 0:
    research = json.loads((REPORTS/'quant-research.json').read_text())
    report['research_warnings'].extend(research['research_warnings'])
    report['walk_forward_results']['research_report'] = 'quant-research.json'
    report['walk_forward_results']['folds'] = research['research']['walk_forward']
    report['parameter_stability']['selected_parameters'] = [w['selected_parameters'] for w in research['research']['walk_forward']]
    report['cost_sensitivity']['folds'] = [w['cost_sensitivity'] for w in research['research']['walk_forward']]
else:
    report['correctness_status'] = 'FAILED'
    report['walk_forward_results']['status'] = 'FAILED'
    report['walk_forward_results']['log'] = 'quant-research.log'
benchmark_run = subprocess.run([sys.executable, 'quant/benchmarks/run_required.py', '--output', str(REPORTS/'quant-benchmarks.json')], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
(REPORTS/'quant-benchmarks.log').write_text(benchmark_run.stdout)
report['benchmarks'] = {'status': 'PASSED' if benchmark_run.returncode == 0 else 'FAILED', 'report': 'quant-benchmarks.json'}
if benchmark_run.returncode:
    report['correctness_status'] = 'FAILED'
(REPORTS/'quant-reliability.json').write_text(json.dumps(report, indent=2)+'\n')
sections = ['golden_oracle_results', 'pit_mutation_results', 'multi_timeframe_results', 'parameter_stability', 'walk_forward_results', 'cost_sensitivity', 'semantic_grader_results']
(REPORTS/'quant-reliability.md').write_text('# Quant Reliability v0.2\n\nCorrectness: **'+report['correctness_status']+'**\n\n'+json.dumps(report['counts'])+'\n\n'+ '\n'.join(f'- {k}: {report[k]["status"]}' for k in sections)+'\n\nFull evidence: quant-regression.log, quant-regression.junit.xml, quant-reliability.json.\n\nSynthetic research does not establish future profitability. Research performance changes are warnings; computational errors fail the gate.\n')
summary_path = REPORTS/'quant-test-summary.json'
summary = json.loads(summary_path.read_text()) if summary_path.exists() else {'schema_version': '0.2'}
summary['reliability'] = {'status': report['correctness_status'], 'report': 'quant-reliability.json', 'counts': report['counts']}
summary_path.write_text(json.dumps(summary, indent=2)+'\n')
subprocess.run(['node', 'scripts/refresh-quant-summary.mjs'], cwd=ROOT, check=True)
sys.exit(run.returncode or research_run.returncode or benchmark_run.returncode)
