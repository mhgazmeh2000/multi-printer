"""Read at most 2 MiB of the existing isolated-copy log; no DB/server access.

Only allowlisted categories and a numeric timestamp are printed. Never emit SQL,
raw log lines, addresses, usernames, paths from log prefixes, or error messages.
"""
import json
from pathlib import Path
import re

LOG = Path(r'E:\PrintGuard-Test\restore-20261110T110905Z-389986ce\postgresql.log')


def summarize(text):
    lines = text.splitlines()
    hits = [i for i, line in enumerate(lines)
            if re.search(r'ERROR:\s+canceling statement due to statement timeout', line, re.I)]
    result = {'scope': 'existing test log only; no server start or DB connection',
              'timeout_found': bool(hits), 'statement_found': False,
              'statement_category': 'not_identified'}
    if not hits:
        return result
    index = hits[-1]
    match = re.search(r'\b\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?', lines[index])
    result['last_timeout_timestamp_timezone_unspecified'] = match.group(0) if match else None
    pid = re.search(r'\[(\d+)\]', lines[index])
    chunks = []
    for line in lines[index+1:]:
        if not chunks:
            marker = re.search(r'\bSTATEMENT:\s*(.*)', line)
            same_pid = not pid or ('['+pid.group(1)+']') in line
            if marker and same_pid:
                chunks.append(marker.group(1))
            elif re.search(r'\b(?:ERROR|FATAL):', line):
                break
        else:
            if re.search(r'\[\d+\]\s+(?:LOG|ERROR|FATAL|DETAIL|HINT|CONTEXT|STATEMENT):', line):
                break
            chunks.append(line)
    if not chunks:
        return result
    statement = re.sub(r'--[^\n]*', '', '\n'.join(chunks)).lstrip()
    categories = (
        (r'CREATE\s+TABLE\s+yield_run_lab\.meta\b', 'install_batch_including_neighbor_index'),
        (r'CREATE\s+INDEX\s+run_lab_snapshot_neighbor\b', 'create_neighbor_index'),
        (r'INSERT\s+INTO\s+yield_run_lab\.boundaries\b', 'build_boundaries_window'),
        (r'INSERT\s+INTO\s+yield_run_lab\.slots\b', 'build_slot_tails'),
        (r'ANALYZE\s+yield_run_lab\.', 'analyze_lab'),
    )
    result['statement_found'] = True
    for pattern, category in categories:
        if re.match(pattern, statement, re.I):
            result['statement_category'] = category
            break
    result['exact_statement_within_batch_known'] = (
        result['statement_category'] not in ('install_batch_including_neighbor_index', 'not_identified'))
    return result


def main():
    try:
        with LOG.open('rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell()-2*1024*1024))
            result = summarize(stream.read().decode('utf-8', errors='replace'))
    except OSError:
        result = {'status': 'TEST_LOG_UNAVAILABLE', 'database_contacted': False}
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
