"""Database lifecycle and SQL APIs; no sample IDs, fixtures, or case assertions.

Callers own the observations dictionary and supply all policies and test SQL.
"""
import ctypes as C
import os
from pathlib import Path
import subprocess


def new_observations(instance_id):
    return {'instance_id': instance_id, 'targets': [], 'controls': [],
            'transcript': [], 'reproduced': False}


def check(observations, group, name, passed, actual, expected):
    observations[group].append({'name': name, 'passed': bool(passed),
                               'actual': actual, 'expected': expected})


class SQLite:

    def __init__(self, observations):
        self.out = observations
        self.lib = C.CDLL('/build/libsqlitepilot.so')
        l = self.lib
        l.sqlite3_libversion.restype = C.c_char_p
        l.sqlite3_sourceid.restype = C.c_char_p
        l.sqlite3_open.argtypes = [C.c_char_p, C.POINTER(C.c_void_p)]
        l.sqlite3_close.argtypes = [C.c_void_p]
        l.sqlite3_errmsg.argtypes = [C.c_void_p]
        l.sqlite3_errmsg.restype = C.c_char_p
        self.row_type = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_int, C.POINTER(C.c_char_p), C.POINTER(C.c_char_p))
        self.auth_type = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_int, C.c_char_p, C.c_char_p, C.c_char_p, C.c_char_p)
        l.sqlite3_exec.argtypes = [C.c_void_p, C.c_char_p, self.row_type, C.c_void_p, C.c_void_p]
        l.sqlite3_set_authorizer.argtypes = [C.c_void_p, self.auth_type, C.c_void_p]
        self.db = C.c_void_p()
        assert l.sqlite3_open(b':memory:', C.byref(self.db)) == 0
        self.events = []
        self.out['database_version'] = l.sqlite3_libversion().decode()
        self.out['database_source_id'] = l.sqlite3_sourceid().decode()

    def sql(self, sql):
        rows = []

        def row(_, n, vals, names):
            rows.append([vals[i].decode() if vals[i] is not None else None for i in range(n)])
            return 0
        rc = self.lib.sqlite3_exec(self.db, sql.encode(), self.row_type(row), None, None)
        r = {'sql': sql, 'rc': rc, 'rows': rows, 'error': self.lib.sqlite3_errmsg(self.db).decode() if rc else ''}
        self.out['transcript'].append(r)
        return r

    def setup(self, sql):
        r = self.sql(sql)
        if r['rc']:
            raise RuntimeError(r)
        return r

    def auth(self, policy=None):
        self.events = []

        def callback(_, op, a, b, db, ctx):
            vals = [x.decode() if x is not None else None for x in [a, b, db, ctx]]
            self.events.append([op, *vals])
            return policy(op, *vals) if policy else 0
        self.callback = self.auth_type(callback)
        assert self.lib.sqlite3_set_authorizer(self.db, self.callback, None) == 0

    def off(self):
        assert self.lib.sqlite3_set_authorizer(self.db, self.auth_type(), None) == 0

class Postgres:

    def __init__(self, observations):
        self.out = observations
        self.bin = '/opt/pg/bin/'
        os.makedirs('/tmp/pgsocket', exist_ok=True)
        p = subprocess.run([self.bin + 'initdb', '-D', '/tmp/pgdata', '--no-locale', '-E', 'UTF8', '--auth=trust'], capture_output=True, text=True)
        self.out['initdb'] = {'exit_code': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr}
        if p.returncode:
            raise RuntimeError('initdb failed')
        p = subprocess.run([self.bin + 'pg_ctl', '-D', '/tmp/pgdata', '-l', '/tmp/postgres.log', '-o', "-k /tmp/pgsocket -c listen_addresses=''", '-w', 'start'], capture_output=True, text=True, timeout=60)
        self.out['server_start'] = {'exit_code': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr}
        if p.returncode:
            raise RuntimeError('pg_ctl failed')

    def sql(self, sql, role='bench'):
        p = subprocess.run([self.bin + 'psql', '-X', '-A', '-t', '-h', '/tmp/pgsocket', '-U', role, '-d', 'postgres', '-v', 'ON_ERROR_STOP=1', '-v', 'VERBOSITY=verbose', '-c', sql], capture_output=True, text=True, timeout=30)
        r = {'role': role, 'sql': sql, 'rc': p.returncode, 'stdout': p.stdout.strip(), 'stderr': p.stderr.strip()}
        self.out['transcript'].append(r)
        return r

    def setup(self, sql, role='bench'):
        r = self.sql(sql, role)
        if r['rc']:
            raise RuntimeError(r)
        return r

    def stop(self):
        subprocess.run([self.bin + 'pg_ctl', '-D', '/tmp/pgdata', '-m', 'immediate', 'stop'], capture_output=True)
        self.out['postgres_log'] = Path('/tmp/postgres.log').read_text(errors='replace')

def permission_denied(r, subject):
    return r['rc'] != 0 and '42501:' in r['stderr'] and (subject in r['stderr'])
