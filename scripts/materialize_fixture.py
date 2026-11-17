"""Write a scenario's synthetic repository to a directory, plus its CI commands as a script.

Used by the `real-logs` workflow to run a fixture exactly as its own CI would, so the
failure log is produced by real pip and Python on a GitHub-hosted runner.
Dependency-free on purpose: it imports only ci_repair.scenarios.

    python scripts/materialize_fixture.py S01 /tmp/fixture
"""
import re
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ci_repair.scenarios import SCENARIOS, WORKFLOW, scenario  # noqa: E402

RUN_LINE = re.compile(r'^\s*- run: (.+)$')


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in SCENARIOS:
        sys.exit(f'usage: materialize_fixture.py {{{",".join(SCENARIOS)}}} DEST')
    files = scenario(sys.argv[1])['files']
    dest = Path(sys.argv[2])
    for path, content in files.items():
        pure = PurePosixPath(path)
        if pure.is_absolute() or '..' in pure.parts:
            sys.exit(f'unsafe fixture path: {path}')
        target = dest.joinpath(*pure.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
    commands = [m.group(1) for m in map(RUN_LINE.match, files[WORKFLOW].splitlines()) if m]
    (dest / '.fixture-commands.sh').write_text('set -x\n' + '\n'.join(commands) + '\n', encoding='utf-8')
    print(f'{sys.argv[1]}: {len(files)} files, {len(commands)} commands -> {dest}')


if __name__ == '__main__':
    main()
