"""Controlled, synthetic CI inputs; no credentials or external repositories."""
WORKFLOW = '.github/workflows/ci.yml'
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
        log = "ModuleNotFoundError: No module named 'requests'"
    elif code == 'S03':
        files[WORKFLOW] = files[WORKFLOW].replace('python -m pytest tests', 'python -m pytests tests')
        log = 'python: No module named pytests'
    elif code == 'S04':  # two independent failures: needs two attempts, one file each
        files[WORKFLOW] = files[WORKFLOW].replace('requirements.txt', 'requirement.txt')
        files['app.py'] = 'from requests import get\n'
        log = ('ERROR: Could not open requirements file: requirement.txt (No such file or directory)\n'
               "ModuleNotFoundError: No module named 'requests'")
    else:
        raise ValueError('Unknown scenario')
    return {'scenario': code, 'files': files, 'log': log, 'attempts': 0,
            'feedback': '', 'status': 'started'}
