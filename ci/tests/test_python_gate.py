"""Python gate failures must not be hidden by otherwise successful test records."""

import pytest

from ci import results, run_pythonlib
from ci._pytest import junit_test_id
from ci._util import Result


def test_python_gate_rejects_a_nonzero_process_even_with_passing_test_records(monkeypatch, tmp_path):
    monkeypatch.setattr(run_pythonlib, 'run_pytest', lambda **kwargs: Result(1, '', ''))
    monkeypatch.setattr(run_pythonlib, 'parse_junit', lambda path: {'test_case.py::test_ok': 'pass'})
    assert run_pythonlib.main(['--evidence-dir', str(tmp_path)]) == 1
    assert results.load('pythonlib', tmp_path)['status'] == 'fail'


def test_python_gate_requires_test_records(monkeypatch, tmp_path):
    monkeypatch.setattr(run_pythonlib, 'run_pytest', lambda **kwargs: Result(0, '', ''))
    monkeypatch.setattr(run_pythonlib, 'parse_junit', lambda path: {})
    assert run_pythonlib.main(['--evidence-dir', str(tmp_path)]) == 1
    assert results.load('pythonlib', tmp_path)['status'] == 'error'


@pytest.mark.parametrize('classname,name,expected', [
    ('tests.test_studio_store', 'test_create', 'test_studio_store.py::test_create'),
    ('tests.test_example.TestGroup', 'test_case', 'test_example.py::TestGroup::test_case'),
    ('package.tests.test_example', 'test_case[value]', 'package/test_example.py::test_case[value]'),
])
def test_junit_identifiers_remain_usable_node_ids(classname, name, expected):
    assert junit_test_id(classname, name) == expected
