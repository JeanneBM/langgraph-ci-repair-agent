"""GitHub Actions log adapter: cleanup, redaction, size limit and read-only download."""
import io
import json
import subprocess
import sys
import urllib.error
from email.message import Message
from pathlib import Path
import pytest
from ci_repair.actions_logs import fetch_job_log, normalize, parse_job_reference

RAW = (
    '﻿2026-11-17T10:00:00.0000000Z ##[group]Run python -m pip install -r requirement.txt\n'
    '2026-11-17T10:00:00.1000000Z python -m pip install -r requirement.txt\n'
    '2026-11-17T10:00:00.2000000Z ##[endgroup]\n'
    "2026-11-17T10:00:01.0000000Z \x1b[31mERROR: Could not open requirements file: requirement.txt\x1b[0m\n"
    '2026-11-17T10:00:01.2000000Z ##[error]Process completed with exit code 1.\n'
)


def test_timestamps_colours_and_markers_are_removed():
    text = normalize(RAW)
    assert text.splitlines() == [
        'Run python -m pip install -r requirement.txt',
        'python -m pip install -r requirement.txt',
        'ERROR: Could not open requirements file: requirement.txt',
        'error: Process completed with exit code 1.',
    ]


@pytest.mark.parametrize('secret', [
    'ghp_' + 'a' * 36, 'github_pat_' + 'B' * 40, 'AKIA' + 'C' * 16,
    'Authorization: Bearer abcdefghijklmnop', 'https://user:hunter2@example.com/repo.git',
    'password=hunter2', 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlc2lnbg',
])
def test_secrets_are_redacted(secret):
    text = normalize('2026-11-17T10:00:00Z leaked ' + secret + '\n')
    assert 'hunter2' not in text and secret not in text and 'REDACTED' in text


def test_long_log_keeps_the_cause_of_the_failed_step():
    noise = ''.join(f'2026-11-17T10:00:00Z downloading part {i} of a large dependency tree\n' for i in range(3000))
    raw = (noise + "2026-11-17T10:00:00Z Error: failed to fetch chart.tgz : 504 Gateway Timeout\n"
           '2026-11-17T10:00:00Z ##[error]Process completed with exit code 1.\n' + noise)
    text = normalize(raw, limit=2000)
    assert len(text) <= 2000
    assert '504 Gateway Timeout' in text and 'exit code 1' in text


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_download_follows_the_redirect_without_forwarding_the_token():
    seen = {}

    class Opener:
        def open(self, request, timeout):
            seen['api'] = request
            headers = Message()
            headers['Location'] = 'https://blob.example/signed?sig=x'
            raise urllib.error.HTTPError(request.full_url, 302, 'Found', headers, None)

    def downloader(request, timeout):
        seen['blob'] = request
        return FakeResponse(RAW.encode())

    raw = fetch_job_log('JeanneBM/langgraph-ci-repair-agent', 42, token='t0ken', opener=Opener(),
                        downloader=downloader)
    assert raw == RAW
    assert seen['api'].full_url.endswith('/repos/JeanneBM/langgraph-ci-repair-agent/actions/jobs/42/logs')
    assert seen['api'].get_header('Authorization') == 'Bearer t0ken'
    assert seen['blob'].get_header('Authorization') is None


@pytest.mark.parametrize('repo,job', [('not a repo', 1), ('owner/repo', 'abc'), ('owner/repo/extra', 1)])
def test_download_rejects_malformed_references(repo, job):
    with pytest.raises(ValueError):
        fetch_job_log(repo, job, opener=object())


def test_job_reference_parsing():
    assert parse_job_reference('JeanneBM/repo:123') == ('JeanneBM/repo', '123')
    with pytest.raises(ValueError):
        parse_job_reference('JeanneBM/repo/123')


def test_cli_runs_on_a_real_log_file_and_records_its_source(tmp_path):
    log = tmp_path / 'job.log'
    log.write_text(RAW, encoding='utf-8')
    out = tmp_path / 'out'
    root = Path(__file__).resolve().parent.parent
    subprocess.run([sys.executable, '-m', 'ci_repair.cli', 'run', '--scenario', 'S01', '--thread', 'real',
                    '--log-file', str(log), '--output', str(out)], check=True, cwd=root, capture_output=True)
    state = json.loads((out / 'real' / 'evidence.json').read_text(encoding='utf-8'))
    assert state['status'] == 'awaiting_approval'
    assert state['log_source'] == 'file:job.log' and len(state['log_sha256']) == 64
    assert state['log'].startswith('Run python -m pip install')
    assert (out / 'real' / 'policy-audit.jsonl').exists()
