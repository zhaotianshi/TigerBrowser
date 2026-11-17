"""Job environment expressions must be valid before GitHub allocates a runner."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def workflow():
    path = Path(__file__).resolve().parents[2] / '.github/workflows/tests.yml'
    return yaml.load(path.read_text(encoding='utf-8'), Loader=yaml.BaseLoader)


def run_workflow_python(step, **environment):
    script = step['run'].split("python - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    return subprocess.run(
        [sys.executable, '-c', script],
        env={**os.environ, **environment},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(('event', 'stage', 'platforms'), [
    ('pull_request', '', {'win-x86_64', 'lin-x86_64', 'mac-arm64', 'mac-x86_64'}),
    ('pull_request', 'server-windows', {'win-x86_64', 'lin-x86_64', 'mac-arm64', 'mac-x86_64'}),
    ('workflow_dispatch', 'full', {'win-x86_64', 'lin-x86_64', 'mac-arm64', 'mac-x86_64'}),
    ('workflow_dispatch', 'server-windows', {'win-x86_64', 'lin-x86_64'}),
    ('workflow_dispatch', 'macos', {'mac-arm64', 'mac-x86_64'}),
])
def test_package_stages_preserve_full_pr_coverage(workflow, tmp_path, event, stage, platforms):
    selector = next(step for step in workflow['jobs']['static']['steps'] if step.get('id') == 'packages')
    output = tmp_path / 'outputs'
    result = run_workflow_python(selector, EVENT=event, STAGE=stage, GITHUB_OUTPUT=str(output))
    assert result.returncode == 0, result.stderr
    matrix = json.loads(output.read_text(encoding='utf-8').removeprefix('matrix='))
    assert {item['platform'] for item in matrix['include']} == platforms
    assert len(matrix['include']) == len(platforms)
    package = workflow['jobs']['client-package']
    assert 'static' in package['needs']
    assert package['strategy']['matrix'] == '${{ fromJSON(needs.static.outputs.packages) }}'
    assert workflow['jobs']['static']['outputs']['packages'] == '${{ steps.packages.outputs.matrix }}'


@pytest.mark.parametrize('stage', ['', 'unknown'])
def test_invalid_manual_stage_fails_before_packaging(workflow, tmp_path, stage):
    selector = next(step for step in workflow['jobs']['static']['steps'] if step.get('id') == 'packages')
    output = tmp_path / 'outputs'
    result = run_workflow_python(selector, EVENT='workflow_dispatch', STAGE=stage, GITHUB_OUTPUT=str(output))
    assert result.returncode != 0
    assert not output.exists()


def test_partial_runs_cannot_report_the_required_full_gate(workflow):
    assert workflow['jobs']['gate']['name'] == (
        "${{ (github.event_name == 'pull_request' || inputs.stage == 'full') "
        "&& 'All tests passed' || format('Selected stage passed ({0})', inputs.stage) }}"
    )
    assert workflow['on']['pull_request'] == ''
    stage = workflow['on']['workflow_dispatch']['inputs']['stage']
    assert stage['default'] == 'server-windows'
    assert stage['options'] == ['server-windows', 'macos', 'full']
    for name, job in workflow['jobs'].items():
        if name != 'gate':
            assert 'if' not in job, f'{name} must run in every stage'


@pytest.mark.parametrize('status', ['success', 'failure', 'cancelled', 'skipped'])
def test_gate_rejects_every_non_success_result(workflow, status):
    gate = workflow['jobs']['gate']
    assert set(gate['needs']) == {'static', 'pythonlib', 'frontend', 'client-package', 'native'}
    assert gate['if'] == 'always()'
    for failed_job in gate['needs']:
        results = {name: {'result': status if name == failed_job else 'success'} for name in gate['needs']}
        result = run_workflow_python(gate['steps'][0], RESULTS=json.dumps(results))
        assert (result.returncode == 0) == (status == 'success'), result.stderr


def test_storage_and_execution_are_bounded(workflow):
    inputs = workflow['on']['workflow_dispatch']['inputs']
    assert 'upload_installers' not in inputs
    for job in workflow['jobs'].values():
        assert 0 < int(job['timeout-minutes']) <= 60
        for step in job['steps']:
            assert not step.get('uses', '').startswith(('actions/upload-artifact@', 'actions/download-artifact@'))
    concurrency = workflow['concurrency']
    assert concurrency['cancel-in-progress'] == 'true'
    assert 'github.event_name' in concurrency['group']
    assert 'inputs.stage' in concurrency['group']


def test_consumers_build_the_frontend_without_artifact_storage(workflow):
    for name in ('pythonlib', 'native'):
        assert any(step.get('uses') == './.github/actions/build-studio-frontend'
                   for step in workflow['jobs'][name]['steps'])
    assert any(step.get('uses') == './.github/actions/package-studio-client'
               for step in workflow['jobs']['client-package']['steps'])


def test_release_builds_use_the_same_verified_packager():
    root = Path(__file__).resolve().parents[2]
    release = yaml.load((root / '.github/workflows/release-studio.yml').read_text(encoding='utf-8'),
                        Loader=yaml.BaseLoader)
    assert set(release['on']) == {'workflow_dispatch'}
    assert release['permissions'] == {}
    assert release['concurrency']['cancel-in-progress'] == 'false'
    jobs = release['jobs']
    assert any(step.get('uses') == './.github/actions/package-studio-client'
               for step in jobs['build']['steps'])
    assert jobs['build']['strategy']['fail-fast'] == 'false'
    assert set(jobs['publish']['needs']) == {'prepare', 'build'}
    assert 'if' not in jobs['publish']
    for name, job in jobs.items():
        assert job['permissions'] == ({'contents': 'write', 'checks': 'read'} if name == 'prepare'
                                      else {'contents': 'write'})
        for step in job['steps']:
            assert not step.get('uses', '').startswith(('actions/upload-artifact@', 'actions/download-artifact@'))
    action = yaml.load((root / '.github/actions/package-studio-client/action.yml').read_text(encoding='utf-8'),
                       Loader=yaml.BaseLoader)
    assert any(step.get('uses') == './.github/actions/build-studio-frontend' for step in action['runs']['steps'])
    assert any('scripts/package-studio.py' in step.get('run', '') for step in action['runs']['steps'])


def test_job_environment_contexts_are_available_before_runner_allocation():
    # https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#context-availability
    allowed = {'github', 'needs', 'strategy', 'matrix', 'vars', 'secrets', 'inputs'}
    root = Path(__file__).resolve().parents[2] / '.github/workflows'
    for path in root.glob('*.yml'):
        workflow = yaml.safe_load(path.read_text(encoding='utf-8'))
        for name, job in workflow.get('jobs', {}).items():
            for expression in re.findall(r'\$\{\{(.*?)\}\}', json.dumps(job.get('env', {}))):
                expression = re.sub(r"'(?:[^']|'')*'", "''", expression)
                contexts = set(re.findall(r'(?<![\w.])([A-Za-z_]\w*)\s*(?:\.|\[)', expression))
                assert contexts <= allowed, f'{path.name}: {name}.env uses unavailable contexts: {contexts - allowed}'
