"""Controlled, synthetic CI inputs; no credentials or external repositories.

The fixtures are real enough to run: the `real-logs` workflow executes each one on a
GitHub-hosted runner, and ci_repair.isolation runs candidates the same way locally.
"""
WORKFLOW = '.github/workflows/ci.yml'
SCENARIOS = ['S01', 'S02', 'S03', 'S04', 'S05', 'S06']
# A test that imports app.py, so a missing dependency really fails the test run.
IMPORTING_TEST = 'import app\n\n\ndef test_app_imports():\n    assert app is not None\n'
BASE = {
    WORKFLOW: 'name: CI\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: python -m pip install -r requirements.txt\n      - run: python -m pytest tests\n',
    'requirements.txt': 'pytest\n',
    'tests/test_app.py': 'def test_sum():\n    assert 2 + 2 == 4\n',
}

def scenario(code):
    files = dict(BASE)
    if code == 'S01':
        files[WORKFLOW] = files[WORKFLOW].replace('requirements.txt', 'requirement.txt')
        log = 'ERROR: Could not open requirements file: requirement.txt (No such file or directory)'
    elif code == 'S02':
        files['app.py'] = 'import requests\n'
        files['tests/test_imports.py'] = IMPORTING_TEST
        log = "ModuleNotFoundError: No module named 'requests'"
    elif code == 'S03':
        files[WORKFLOW] = files[WORKFLOW].replace('python -m pytest tests', 'python -m pytests tests')
        log = 'python: No module named pytests'
    elif code == 'S04':  # two independent failures: needs two attempts, one file each
        files[WORKFLOW] = files[WORKFLOW].replace('requirements.txt', 'requirement.txt')
        files['app.py'] = 'from requests import get\n'
        files['tests/test_imports.py'] = IMPORTING_TEST
        log = ('ERROR: Could not open requirements file: requirement.txt (No such file or directory)\n'
               "ModuleNotFoundError: No module named 'requests'")
    elif code == 'S05':
        files[WORKFLOW] = (files[WORKFLOW].replace('requirements.txt', 'requirement.txt')
                           .replace('python -m pytest tests', 'python -m pytests tests'))
        files['app.py'] = 'from requests import get\n'
        files['tests/test_imports.py'] = IMPORTING_TEST
        log = ('ERROR: Could not open requirements file: requirement.txt (No such file or directory)\n'
               'python: No module named pytests\n'
               "ModuleNotFoundError: No module named 'requests'")
    elif code == 'S06':  # out of scope: a failing assertion; tests are protected, so escalate
        files['tests/test_app.py'] = 'def test_sum():\n    assert 2 + 2 == 5\n'
        log = 'FAILED tests/test_app.py::test_sum - assert (2 + 2) == 5'
    else:
        raise ValueError('Unknown scenario')
    return {'scenario': code, 'files': files, 'log': log, 'attempts': 0,
            'feedback': '', 'status': 'started'}
