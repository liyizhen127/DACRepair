"""Small CLI-based SQL scenario runner for authored, source-grounded regression cases.

The database process is supplied by the case environment. Credentials and endpoints
are configurable through ACREPAIR_* environment variables. This runner does not
start a database server and its presence is not proof that an adapter is ready.
"""
import json
import os
from pathlib import Path
import subprocess


def _cmd(scenario, query, role):
    driver = scenario['driver']
    sql = query['sql']
    if driver == 'clickhouse':
        if role == 'root': role = os.environ.get('ACREPAIR_ADMIN_USER', 'default')
        return ['clickhouse-client', '--format', 'TabSeparatedRaw', '--user', role, '--query', sql]
    if driver in ('mysql', 'mariadb', 'tidb', 'starrocks'):
        if role == 'root': role = os.environ.get('ACREPAIR_ADMIN_USER', 'root')
        binary = os.environ.get('ACREPAIR_MYSQL_CLIENT', 'mariadb')
        argv = [binary, '--batch', '--skip-column-names', '--user', role]
        password = os.environ.get('ACREPAIR_PASSWORD_' + role.upper())
        if password is None: password = scenario.get('credentials', {}).get(role)
        if password is not None:
            argv.append('--password=' + password)
        host = os.environ.get('ACREPAIR_HOST', '')
        port = os.environ.get('ACREPAIR_PORT', '')
        socket = os.environ.get('ACREPAIR_SOCKET', '')
        if host: argv += ['--host', host]
        if port: argv += ['--port', port]
        if socket: argv += ['--socket', socket]
        database = query.get('database')
        if database: argv += ['--database', database]
        argv += ['--execute', sql]
        return argv
    if driver == 'firebird':
        if role == 'root': role = os.environ.get('ACREPAIR_ADMIN_USER', 'SYSDBA')
        binary = os.environ.get('ACREPAIR_ISQL', 'isql')
        dsn = os.environ.get('ACREPAIR_FIREBIRD_DSN')
        if not dsn: raise RuntimeError('ACREPAIR_FIREBIRD_DSN is required')
        password = os.environ.get('ACREPAIR_PASSWORD_' + role.upper())
        if password is None: password = scenario.get('credentials', {}).get(role, 'masterkey')
        argv = [binary, '-user', role, '-password', password]
        active_role = query.get('active_role')
        if active_role:
            argv += ['-role', active_role]
        return argv + [dsn]
    raise RuntimeError('No SQL CLI for driver: ' + driver)


def _run(scenario, query, role):
    argv = _cmd(scenario, query, role)
    payload = ('SET BAIL ON;\n' + query['sql'] + '\n') if scenario['driver'] == 'firebird' else None
    timeout = int(scenario.get('query_timeout_seconds', 60))
    try:
        p = subprocess.run(argv, input=payload, capture_output=True, text=True, timeout=timeout)
        return {'role': role, 'sql': query['sql'], 'returncode': p.returncode,
                'stdout': p.stdout, 'stderr': p.stderr, 'command': argv}
    except subprocess.TimeoutExpired as exc:
        return {'role': role, 'sql': query['sql'], 'returncode': 124,
                'stdout': exc.stdout or '', 'stderr': 'SQL_TIMEOUT', 'command': argv,
                'infrastructure_error': 'SQL_TIMEOUT'}


def _matches(actual, expected):
    if not isinstance(expected, dict) or not expected: return False
    if 'success' in expected and ((actual['returncode'] == 0) != expected['success']): return False
    if 'returncode' in expected and actual['returncode'] != expected['returncode']: return False
    if 'stdout_contains' in expected and expected['stdout_contains'] not in actual['stdout']: return False
    if 'stdout_not_contains' in expected and expected['stdout_not_contains'] in actual['stdout']: return False
    if 'stdout_empty' in expected and (not isinstance(expected['stdout_empty'], bool) or (not actual['stdout'].strip()) != expected['stdout_empty']): return False
    if 'stderr_contains' in expected and expected['stderr_contains'] not in actual['stderr'] + actual['stdout']: return False
    return True


def main():
    scenario = json.loads(Path('/checks/scenario.json').read_text())
    checks=[]; targets=[]; transcript=[]
    for step in scenario['steps']:
        actual=[]
        for query in step['queries']:
            result=_run(scenario,query,query.get('role',step.get('role','root')))
            transcript.append(result); actual.append(result)
            if result.get('infrastructure_error'):
                print('CHECK_RESULT='+json.dumps({
                    'harness_error': result['infrastructure_error'],
                    'origin': scenario['origin'],
                    'transcript': transcript,
                },ensure_ascii=False),flush=True)
                return
        good=all(_matches(a,q['expect']) for a,q in zip(actual,step['queries']))
        if step['kind']=='setup':
            if not good: raise RuntimeError('SQL_SETUP_FAILED: '+json.dumps(actual,ensure_ascii=False))
            continue
        checks.append({'name':step['name'],'kind':step['kind'],'passed':good,
                       'actual':actual,'expected':[q['expect'] for q in step['queries']]})
        if step['kind']=='target':
            targets.append(not good and all(_matches(a,q['bug']) for a,q in zip(actual,step['queries'])))
    controls=all(x['passed'] for x in checks if x['kind']=='control')
    print('CHECK_RESULT='+json.dumps({'checks':checks,'reproduced':bool(targets) and all(targets) and controls,
          'origin':scenario['origin'],'transcript':transcript},ensure_ascii=False),flush=True)


if __name__=='__main__': main()
