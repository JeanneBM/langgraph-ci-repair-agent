"""Run a fixture's own CI commands against a candidate snapshot, outside the agent process.

The host turns the candidate's workflow into a plan of recognised command shapes.
Anything else is refused before execution. Both backends run the same in-sandbox
driver, so they produce the same kind of log:

* ``venv``   - temporary copy, fresh virtual environment, clean environment variables
               (no tokens or cloud credentials). Process isolation only.
* ``docker`` - throwaway container: no network, read-only root file system, no Linux
               capabilities, non-root user, CPU/memory/PID limits. Packages come only
               from a pre-fetched wheelhouse mounted read-only.

The output is the real interpreter and pip output, so it can serve as the next
failure log. It is still untrusted data: it can only trigger the fixed repair rules.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
import yaml
from .guard import snapshot_sha256
from .scenarios import WORKFLOW

ALLOWED_REQUIREMENTS = {'pytest', 'requests'}
REQUIREMENT_LINE = re.compile(r'^([A-Za-z0-9_.-]+)\s*(==\s*[A-Za-z0-9_.+!-]+)?$')
IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
PYTEST_ARG = re.compile(r'^[A-Za-z0-9_./:=-]+$')
PYTEST_FORBIDDEN = {'-p', '-c', '-o', '--pyargs', '--rootdir', '--confcutdir', '--override-ini', '--basetemp'}
NEVER_PROBE = {'pip', 'ensurepip', 'venv', 'pytest'}
PROXY_VARIABLES = ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'NO_PROXY', 'no_proxy',
                   'SSL_CERT_FILE', 'PIP_CERT', 'REQUESTS_CA_BUNDLE')
MAX_LOG_CHARS = 6000
TOTAL_TIMEOUT = 600

# Executed inside the sandbox by a trusted interpreter. The plan arrives as JSON in an
# environment variable; nothing from the candidate is ever passed to a shell.
DRIVER = r'''
import importlib.util, json, os, shlex, subprocess, sys, venv
plan = json.loads(os.environ['CI_REPAIR_PLAN'])
work, env_dir = os.environ['CI_REPAIR_WORK'], os.environ['CI_REPAIR_VENV']
venv.create(env_dir, with_pip=True)
py = os.path.join(env_dir, 'Scripts' if os.name == 'nt' else 'bin', 'python.exe' if os.name == 'nt' else 'python')
probe = 'import importlib.util, sys; sys.exit(3 if importlib.util.find_spec(sys.argv[1]) else 0)'  # cwd counts
for step in plan:
    print('$ ' + shlex.join(step['args']), flush=True)
    if step['kind'] == 'probe' and subprocess.run([py, '-c', probe, step['args'][2]], cwd=work).returncode:
        print('__REFUSED__ module exists in the sandbox: ' + step['args'][2], flush=True)
        sys.exit(0)
    code = subprocess.run([py] + step['argv'][1:], cwd=work, stderr=subprocess.STDOUT).returncode
    if code:
        print('__EXIT__ %d' % code, flush=True)
        sys.exit(0)
print('__EXIT__ 0', flush=True)
'''


class Refused(ValueError):
    """The candidate asks for something the isolated runner will not execute."""


@dataclass(frozen=True)
class IsolatedResult:
    status: str            # passed | failed | refused | error
    backend: str
    snapshot_sha256: str
    log: str = ''
    detail: str = ''

    def as_state(self):
        return asdict(self)


def _safe_path(path):
    pure = PurePosixPath(path)
    if (not path or pure.is_absolute() or '..' in pure.parts or '\\' in path
            or ':' in path or path.startswith('-')):
        raise Refused(f'Unsafe path: {path[:80]}')
    return pure


def _requirement_names(text):
    names = []
    for raw in text.splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        match = REQUIREMENT_LINE.match(line)
        if not match:
            raise Refused('Requirements may only list allow-listed package names.')
        names.append(match.group(1).lower().replace('_', '-'))
    return names


def command_plan(files):
    """Recognised command shapes from the candidate's workflow, in order, or Refused."""
    for path in files:
        _safe_path(path)
    try:
        workflow = yaml.safe_load(files[WORKFLOW])
        steps = workflow['jobs']['test']['steps']
    except (KeyError, TypeError, yaml.YAMLError) as exc:
        raise Refused(f'Workflow is not runnable: {type(exc).__name__}') from None
    plan = []
    for step in steps:
        for command in (ln.strip() for ln in str(step.get('run', '')).splitlines()):
            if not command:
                continue
            args = shlex.split(command)
            if args[:4] == ['python', '-m', 'pip', 'install']:
                if args[4:5] != ['-r'] or len(args) != 6:
                    raise Refused('Only `python -m pip install -r <file>` is run in isolation.')
                _safe_path(args[5])
                if args[5] in files and any(n not in ALLOWED_REQUIREMENTS for n in _requirement_names(files[args[5]])):
                    raise Refused('Requirements outside the allow-list are not installed.')
                plan.append({'kind': 'pip', 'args': args})
            elif args[:3] == ['python', '-m', 'pytest']:
                extra = args[3:]
                if any(a in PYTEST_FORBIDDEN or a.startswith('-p') or not PYTEST_ARG.match(a) for a in extra):
                    raise Refused('Unsupported pytest arguments.')
                plan.append({'kind': 'pytest', 'args': args})
            elif args[:2] == ['python', '-m'] and len(args) >= 3 and IDENTIFIER.match(args[2]) \
                    and args[2] not in NEVER_PROBE and all(PYTEST_ARG.match(a) for a in args[3:]):
                # Run only if the module does not exist in the sandbox, so it can only fail.
                plan.append({'kind': 'probe', 'args': args})
            else:
                raise Refused('Command shape not runnable in isolation: ' + command[:80])
    if not plan:
        raise Refused('Workflow has no run commands.')
    return plan


def _with_argv(plan, wheelhouse):
    for step in plan:
        if step['kind'] == 'pip':
            index = ['--no-index', '--find-links', wheelhouse] if wheelhouse else []
            step['argv'] = ['python', '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
                            '--only-binary=:all:', *index, '-r', step['args'][5]]
        else:
            step['argv'] = list(step['args'])
    return plan


def materialize(files, root):
    for path, content in files.items():
        target = root.joinpath(*_safe_path(path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8', newline='\n')


def bounded(text, limit=MAX_LOG_CHARS):
    if len(text) <= limit:
        return text
    head = limit // 4
    return text[:head] + '\n[... truncated ...]\n' + text[-(limit - head):]


def _result_from_output(output, backend, sha, scrub):
    lines = [scrub(line) for line in output.replace('\r\n', '\n').split('\n')]
    markers = [ln for ln in lines if ln.startswith(('__EXIT__ ', '__REFUSED__ '))]
    log = bounded('\n'.join(ln for ln in lines if not ln.startswith(('__EXIT__ ', '__REFUSED__ '))).strip())
    if not markers:
        return IsolatedResult('error', backend, sha, log, 'Sandbox ended without a result.')
    last = markers[-1]
    if last.startswith('__REFUSED__ '):
        return IsolatedResult('refused', backend, sha, log, last[len('__REFUSED__ '):])
    code = int(last.split()[1])
    return IsolatedResult('passed' if code == 0 else 'failed', backend, sha, log,
                          '' if code == 0 else f'exit code {code}')


class VenvRunner:
    """Process isolation: temp copy, new venv, minimal environment, per-run timeout."""
    name = 'venv'

    def __init__(self, wheelhouse=None, timeout=TOTAL_TIMEOUT):
        self.wheelhouse = str(Path(wheelhouse).resolve()) if wheelhouse else None
        self.timeout = timeout

    def environment(self, tmp):
        env = {'HOME': str(tmp), 'TMPDIR': str(tmp), 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1',
               'PYTHONNOUSERSITE': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1', 'PIP_NO_INPUT': '1',
               'PIP_CONFIG_FILE': os.devnull}
        if os.name == 'nt':
            system_root = os.environ.get('SYSTEMROOT', r'C:\Windows')
            env.update({'SYSTEMROOT': system_root, 'TEMP': str(tmp), 'TMP': str(tmp), 'USERPROFILE': str(tmp),
                        'PATH': os.path.join(system_root, 'System32')})
        else:
            env['PATH'] = '/usr/bin:/bin'
        if not self.wheelhouse:  # an index needs the network: pass only proxy and CA settings
            env.update({k: os.environ[k] for k in PROXY_VARIABLES if k in os.environ})
        return env

    def run(self, files):
        sha = snapshot_sha256(files)
        try:
            plan = _with_argv(command_plan(files), self.wheelhouse)
        except Refused as exc:
            return IsolatedResult('refused', self.name, sha, '', str(exc))
        tmp = Path(tempfile.mkdtemp(prefix='ci-repair-isolated-'))
        try:
            work = tmp / 'repo'
            materialize(files, work)
            env = self.environment(tmp)
            env.update({'CI_REPAIR_PLAN': json.dumps(plan), 'CI_REPAIR_WORK': str(work),
                        'CI_REPAIR_VENV': str(tmp / 'venv')})
            proc = subprocess.run([sys.executable, '-I', '-c', DRIVER], cwd=work, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, errors='replace', timeout=self.timeout)
            def scrub(line):
                line = line.replace(str(tmp), '<sandbox>')
                return line.replace(self.wheelhouse, '<wheelhouse>') if self.wheelhouse else line
            return _result_from_output(proc.stdout, self.name, sha, scrub)
        except subprocess.TimeoutExpired:
            return IsolatedResult('error', self.name, sha, '', f'Timed out after {self.timeout}s.')
        except OSError as exc:
            return IsolatedResult('error', self.name, sha, '', type(exc).__name__)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class DockerRunner:
    """Container isolation: no network, read-only root, no capabilities, non-root, limits."""
    name = 'docker'

    def __init__(self, wheelhouse, image='python:3.12-slim', docker='docker', timeout=TOTAL_TIMEOUT):
        if not wheelhouse:
            raise ValueError('The docker backend has no network; pass --wheelhouse.')
        self.wheelhouse = str(Path(wheelhouse).resolve())
        self.image, self.docker, self.timeout = image, docker, timeout

    def command(self, name, work, plan):
        return [self.docker, 'run', '--rm', '--name', name, '--network', 'none', '--read-only',
                '--tmpfs', '/tmp:rw,exec,nosuid,size=512m', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges', '--pids-limit', '256', '--memory', '1g', '--cpus', '1',
                '--user', '65534:65534', '-e', 'HOME=/tmp', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                '-e', 'PIP_DISABLE_PIP_VERSION_CHECK=1', '-e', 'PIP_NO_INPUT=1',
                '-e', 'CI_REPAIR_WORK=/work', '-e', 'CI_REPAIR_VENV=/tmp/venv',
                '-e', 'CI_REPAIR_PLAN=' + json.dumps(plan),
                '-v', f'{work}:/work:rw', '-v', f'{self.wheelhouse}:/wheels:ro', '-w', '/work',
                self.image, 'python', '-I', '-c', DRIVER]

    def run(self, files):
        sha = snapshot_sha256(files)
        try:
            plan = _with_argv(command_plan(files), '/wheels')
        except Refused as exc:
            return IsolatedResult('refused', self.name, sha, '', str(exc))
        tmp = Path(tempfile.mkdtemp(prefix='ci-repair-isolated-'))
        name = 'ci-repair-' + uuid.uuid4().hex[:12]
        try:
            work = tmp / 'repo'
            materialize(files, work)
            for path in [work, *work.rglob('*')]:  # the container user (nobody) must be able to write caches
                path.chmod(0o777 if path.is_dir() else 0o666)
            proc = subprocess.run(self.command(name, work, plan), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, errors='replace', timeout=self.timeout)
            return _result_from_output(proc.stdout, self.name, sha, lambda line: line)
        except subprocess.TimeoutExpired:
            subprocess.run([self.docker, 'kill', name], capture_output=True)
            return IsolatedResult('error', self.name, sha, '', f'Timed out after {self.timeout}s.')
        except OSError as exc:
            return IsolatedResult('error', self.name, sha, '', type(exc).__name__)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def runner_for(backend, wheelhouse=None, image='python:3.12-slim'):
    if backend in (None, 'none'):
        return None
    if backend == 'venv':
        return VenvRunner(wheelhouse)
    if backend == 'docker':
        return DockerRunner(wheelhouse, image=image)
    raise ValueError(f'Unknown isolation backend: {backend}')
