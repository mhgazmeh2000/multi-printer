"""Build a self-contained inline PowerShell block from reviewed lab sources.

No network, credentials, telemetry or database connection. Embeds canonical SQL
and functions verbatim, plus a captured reference SELECT (no app import).
"""
import argparse
import ast
import inspect
from pathlib import Path

from tools import yield_run_prototype as prototype
from tools.yield_query_lab import loader_query


def standalone_python():
    base = Path(__file__).resolve().parent
    runner = (base/'yield_copy_stage2.py').read_text(encoding='utf-8')
    dependency = 'from tools.yield_run_prototype import COLUMNS, install, rebuild, reader_query, reference_query'
    if runner.count(dependency) != 1:
        raise RuntimeError('RUNNER_IMPORT_CHANGED')
    reference, _ = loader_query()
    needle = 'FROM toner_snapshots_v2 '
    if needle not in reference:
        raise RuntimeError('REFERENCE_TABLE_TEMPLATE_CHANGED')
    reference = reference.replace(needle, needle+'__WHERE__ ', 1)
    sql = (base/'sql'/'yield_run_prototype.sql').read_text(encoding='utf-8')
    if "'''" in sql or "'''" in reference:
        raise RuntimeError('REVIEW_EMBEDDED_STRING_DELIMITERS')
    payload = ('COLUMNS = '+repr(prototype.COLUMNS)+'\n'
               + "INSTALL_SQL = r'''"+sql+"'''\n"
               + "REFERENCE_SQL = r'''"+reference+"'''\n"
               + inspect.getsource(prototype.guard)+'\n'
               + inspect.getsource(prototype.rebuild)+'\n'
               + '''def install(conn):
    guard(conn)
    conn.execute(INSTALL_SQL)


def reference_query(*, ip=None, color=None, limit=400):
    clauses, params = [], []
    if ip:
        clauses.append("printer_ip = ?")
        params.append(ip)
    if color:
        clauses.append("LOWER(color) = ?")
        params.append(color.lower())
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, int(limit)))
    query = REFERENCE_SQL.replace('__WHERE__', where)
    return query.replace('toner_snapshots_v2', 'yield_run_lab.toner_snapshots_v2').replace('?', '%s'), params

'''
               + inspect.getsource(prototype.reader_query))
    program = runner.replace(dependency, payload)
    ast.parse(program)
    if '\nfrom tools' in program or '\nimport tools' in program:
        raise RuntimeError('INLINE_BLOCK_MUST_NOT_IMPORT_REPOSITORY')
    return program


def powershell_block():
    program = standalone_python()
    if any(line.strip()=="'@" for line in program.splitlines()):
        raise RuntimeError('POWERSHELL_HERE_STRING_COLLISION')
    return '''$ErrorActionPreference = 'Stop'
$Python = if (Get-Command python -ErrorAction SilentlyContinue) {
    'python'
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    'py'
} else {
    throw 'Python was not found.'
}
@'
'''+program+'''\n'@ | & $Python -
if ($LASTEXITCODE -ne 0) {
    throw 'Stage 2 stopped. Send only the JSON summary, not private plans, logs or credentials.'
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.write_text(powershell_block(), encoding='utf-8')
    print('Standalone block written; no database connection was made.')


if __name__=='__main__':
    main()
