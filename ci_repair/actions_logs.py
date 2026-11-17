"""Read-only adapter for GitHub Actions job logs.

Turns a raw job log (timestamps, ANSI colours, ``##[group]`` markers, runner preamble)
into a short, redacted failure log for the agent. The result is still untrusted data:
it can only trigger the fixed repair rules, never instructions.
"""
import json
import os
import re
import urllib.error
import urllib.request

API = 'https://api.github.com'
USER_AGENT = 'langgraph-ci-repair-agent'
MAX_LOG_CHARS = 6000
REPO = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
TIMESTAMP = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?')
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
MARKERS = (('##[group]', ''), ('##[error]', 'error: '), ('##[warning]', 'warning: '),
           ('##[notice]', 'notice: '), ('##[command]', ''), ('##[debug]', 'debug: '))
SECRETS = [
    (re.compile(r'\bgh[pousr]_[A-Za-z0-9]{20,}'), '[REDACTED_GITHUB_TOKEN]'),
    (re.compile(r'\bgithub_pat_[A-Za-z0-9_]{20,}'), '[REDACTED_GITHUB_TOKEN]'),
    (re.compile(r'\bAKIA[0-9A-Z]{16}\b'), '[REDACTED_AWS_KEY]'),
    (re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'), '[REDACTED_JWT]'),
    (re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}'), 'Bearer [REDACTED]'),
    (re.compile(r'(?i)(https?://)[^/\s:@]+:[^/\s@]+@'), r'\1[REDACTED]@'),
    (re.compile(r'(?i)\b(password|passwd|secret|token|api[_-]?key|client[_-]?secret|connection[_-]?string)'
                r'(\s*[=:]\s*)[^\s,;]+'), r'\1\2[REDACTED]'),
]
ERROR_HINT = re.compile(r'(^error: |\berror\b|Traceback|No module named|FAILED|exit code|AssertionError|Exception)',
                        re.IGNORECASE)


def redact(line):
    for pattern, replacement in SECRETS:
        line = pattern.sub(replacement, line)
    return line


def clean_lines(raw):
    """Cleaned lines plus the indexes of step failures (`##[error]` annotations)."""
    lines, failures = [], []
    for line in raw.lstrip('\ufeff').splitlines():
        line = ANSI.sub('', TIMESTAMP.sub('', line.lstrip('\ufeff'))).rstrip()
        if line.startswith('##[endgroup]'):
            continue
        if line.startswith('##[error]'):
            failures.append(len(lines))
        for marker, replacement in MARKERS:
            if line.startswith(marker):
                line = replacement + line[len(marker):]
                break
        lines.append(redact(line))
    return lines, failures


def normalize(raw, limit=MAX_LOG_CHARS, cause_lines=20):
    """Readable, redacted, bounded text.

    A long log keeps, in this order of priority: the lines leading up to each failed
    step (the cause usually sits right above GitHub's error annotation), then the
    lines around other error-looking lines, as far as the size limit allows."""
    lines, failures = clean_lines(raw)
    text = '\n'.join(lines).strip()
    if len(text) <= limit:
        return text
    windows = [range(max(0, i - cause_lines), i + 1) for i in failures]
    windows += [range(max(0, i - 3), min(len(lines), i + 4))
                for i, line in enumerate(lines) if ERROR_HINT.search(line)]
    keep, size = set(), 0
    for window in windows:
        extra = [i for i in window if i not in keep]
        cost = sum(len(lines[i]) + 1 for i in extra) + 6
        if size + cost > limit:
            continue
        keep.update(extra)
        size += cost
    excerpt, previous = [], -2
    for i in sorted(keep):
        if i != previous + 1:
            excerpt.append('[...]')
        excerpt.append(lines[i])
        previous = i
    return '\n'.join(excerpt).strip() if excerpt else '[...]\n' + text[-(limit - 6):]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _token(token):
    return token or os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')


def _api_request(path, token=None):
    headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
               'User-Agent': USER_AGENT}
    token = _token(token)
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return urllib.request.Request(API + path, headers=headers)


def api_json(path, token=None, opener=None):
    opener = opener or urllib.request.build_opener()
    with opener.open(_api_request(path, token), timeout=30) as response:
        return json.loads(response.read().decode('utf-8'))


def fetch_job_log(repo, job_id, token=None, opener=None, downloader=None):
    """Download one job's raw log. Needs a token with read access to Actions.

    GitHub answers with a redirect to a short-lived signed URL; the token is not
    forwarded there."""
    if not REPO.match(repo) or not str(job_id).isdigit():
        raise ValueError('Expected OWNER/REPO and a numeric job ID.')
    opener = opener or urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(_api_request(f'/repos/{repo}/actions/jobs/{job_id}/logs', token), timeout=30) as response:
            return response.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as error:
        if error.code not in (301, 302, 303, 307, 308):
            raise
        location = error.headers.get('Location', '')
    if not location.startswith('https://'):
        raise ValueError('Unexpected log download location.')
    download = downloader or urllib.request.urlopen
    with download(urllib.request.Request(location, headers={'User-Agent': USER_AGENT}), timeout=60) as response:
        return response.read().decode('utf-8', errors='replace')


def parse_job_reference(reference):
    """`OWNER/REPO:JOB_ID` as printed in a job URL (.../actions/runs/<run>/job/<JOB_ID>)."""
    repo, _, job_id = reference.rpartition(':')
    if not REPO.match(repo) or not job_id.isdigit():
        raise ValueError('Use OWNER/REPO:JOB_ID, for example JeanneBM/langgraph-ci-repair-agent:123456789')
    return repo, job_id
