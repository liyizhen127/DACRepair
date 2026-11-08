"""Docker CLI backend. Final agent images never inherit legacy answer layers."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request
import uuid
from .util import sha, save, now
from .models import RuntimeResources


def docker(args, timeout=600, check=True):
    p = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError(f'Docker command failed: {args[:4]}\n{p.stderr[-4000:]}')
    return p


def inspect_image(name):
    p = docker(['image', 'inspect', name], check=False)
    return json.loads(p.stdout)[0] if p.returncode == 0 else None


def prepare_source(case):
    record = case._record
    base = record['base_commit']
    folder = case.root / '.cache' / 'sources' / base
    archive = folder / 'source.tar.gz'
    receipt = folder / 'receipt.json'
    source = folder / 'source'
    folder.mkdir(parents=True, exist_ok=True)
    if receipt.exists() and archive.exists() and source.exists():
        meta = json.loads(receipt.read_text())
        if meta['base_commit'] == base and meta['archive_sha256'] == sha(archive.read_bytes()):
            return source, meta
    # Existing immutable downloads can be reused. No extracted tree with tests applied is trusted.
    prior = list((case.root / 'validation').glob('*/downloads/' + base + '.tar.gz'))
    if prior and not archive.exists():
        shutil.copy2(prior[0], archive)
    url = f'https://codeload.github.com/{record["repo"]}/tar.gz/{base}'
    if not archive.exists():
        with urllib.request.urlopen(url, timeout=180) as response:
            payload = response.read()
        archive.write_bytes(payload)
    if source.exists():
        shutil.rmtree(source)
    unpack = folder / 'unpack'
    if unpack.exists():
        shutil.rmtree(unpack)
    unpack.mkdir()
    with tarfile.open(archive) as tf:
        tf.extractall(unpack, filter='data')
    entries = list(unpack.iterdir())
    if len(entries) != 1 or not entries[0].name.endswith(base):
        raise ValueError('Archive root does not match requested full commit')
    entries[0].rename(source)
    unpack.rmdir()
    meta = {'repo': record['repo'], 'base_commit': base, 'url': url,
            'archive_sha256': sha(archive.read_bytes()), 'retrieved_at': now()}
    save(receipt, meta)
    return source, meta


def build_image(case):
    spec = case.manifest['environment_spec']
    if spec.get('adapter_status') != 'implemented':
        raise RuntimeError('Environment adapter is not implemented for this case')
    record = case._record
    toolchain = spec['toolchain']
    tool = inspect_image(toolchain)
    if not tool:
        dockerfile = case.root / 'benchmark' / 'environments' / 'Dockerfile.toolchain'
        p = subprocess.run(['docker', 'build', '--platform', spec['platform'], '-t', toolchain,
                            '-f', str(dockerfile), str(dockerfile.parent)], capture_output=True, text=True, timeout=3600)
        if p.returncode:
            raise RuntimeError('Toolchain build failed: ' + p.stdout[-2000:] + p.stderr[-2000:])
        tool = inspect_image(toolchain)
    runtime = case.root / 'benchmark' / 'runtime'
    key = sha(json.dumps({'spec': spec, 'base': record['base_commit'], 'toolchain_id': tool['Id'],
                         'builder': sha(Path(__file__).read_bytes()),
                         'build_script': sha((runtime / 'build.sh').read_bytes())}, sort_keys=True).encode())
    tag = f'acrepair-env:{spec["engine"]}-{record["base_commit"][:12]}-{key[:12]}'
    existing = inspect_image(tag)
    receipt_path = case.root / '.cache' / 'images' / (key + '.json')
    if existing and receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if receipt.get('image_id') == existing['Id']:
            return receipt
    source, source_receipt = prepare_source(case)
    context = case.root / '.cache' / 'builds' / key
    context.mkdir(parents=True, exist_ok=True)
    target = context / 'source'
    if not target.exists():
        shutil.copytree(source, target, copy_function=os.link)
    shutil.copy2(runtime / 'build.sh', context / 'build.sh')
    save(context / 'source.json', source_receipt)
    donor = spec.get('prebuilt_donor')
    donor_info = inspect_image(donor['image_id']) if donor else None
    lines = []
    if donor_info:
        if donor.get('reuse_audit') != 'test_patch_only_uncompiled_sql_tcl':
            raise RuntimeError('Prebuilt artifacts lack a source/test isolation audit')
        donor_tag = f'acrepair-artifacts:{record["base_commit"][:16]}'
        docker(['tag', donor_info['Id'], donor_tag])
        lines.append(f'FROM {donor_tag} AS compiled')
    lines += [f'FROM {toolchain}',
              'ARG MAKE_JOBS=12',
              f'ENV BASE_COMMIT={record["base_commit"]} BENCH_ENGINE={spec["engine"]}',
              f'LABEL org.acrepair.base_commit="{record["base_commit"]}" org.acrepair.environment_key="{key}"',
              'USER root', 'RUN mkdir -p /opt/bench && chown bench:bench /opt/bench', 'USER bench',
              'COPY --chown=bench:bench source/ /src/',
              'COPY --chown=bench:bench build.sh source.json /opt/bench/']
    if donor_info:
        # COPY transfers only compiled files. None of the donor's /checks or image layers is inherited.
        lines += ['COPY --from=compiled --chown=bench:bench /build/ /build/',
                  'COPY --from=compiled --chown=bench:bench /opt/pg/ /opt/pg/']
    else:
        lines.append('RUN bash /opt/bench/build.sh initial')
    lines += ['RUN test ! -e /checks/gold_patch.diff && test ! -e /checks/record.json && test ! -e /checks/test_patch.diff',
              'WORKDIR /src']
    df = context / 'Dockerfile'
    df.write_text('\n'.join(lines) + '\n')
    log = context / 'build.log'
    with log.open('w') as out:
        p = subprocess.run(['docker', 'build', '--platform', spec['platform'], '--network=none',
                            '--build-arg', f'MAKE_JOBS={case.resources.jobs}',
                            '-t', tag, '-f', str(df), str(context)], stdout=out, stderr=subprocess.STDOUT, timeout=5400)
    if p.returncode:
        raise RuntimeError(f'Base image build failed; see {log}')
    receipt = {'tag': tag, 'image_id': inspect_image(tag)['Id'], 'environment_key': key,
               'toolchain_id': tool['Id'], 'source': source_receipt, 'created_at': now(),
               'build_log': str(log.relative_to(case.root)),
               'build_jobs': case.resources.jobs,
               'compiled_artifact_donor': donor_info['Id'] if donor_info else None,
               'answer_materials_included': False}
    save(receipt_path, receipt)
    return receipt


class DockerEnvironment:
    """Agent interface: isolated source workspace, execute(), get_patch(), close()."""
    def __init__(self, image, spec, instance_id, *, resources=None):
        self.image = image
        self.resources = resources if resources is not None else RuntimeResources()
        self.name = 'acrepair-agent-' + uuid.uuid4().hex[:16]
        self.instance_id = instance_id
        self.closed = False
        docker(['run', '-d', '--name', self.name, '--platform', spec['platform'], '--network=none',
                *self.resources.docker_run_args(), '--shm-size=1g',
                '--workdir=/src', image['image_id'], 'sleep', 'infinity'])
        try:
            # This is a local diff baseline; upstream provenance is /opt/bench/source.json.
            result = self.execute("git init -q && git config user.email benchmark@local.invalid && "
                                  "git config user.name Benchmark && git add -f . && "
                                  "git -c commit.gpgsign=false commit -qm 'Local diff baseline; upstream SHA is in /opt/bench/source.json'")
            if result['returncode']:
                raise RuntimeError(result['output'])
        except Exception:
            self.close()
            raise

    def execute(self, command, timeout=600):
        if self.closed:
            raise RuntimeError('Environment is closed')
        try:
            p = docker(['exec', '--workdir=/src', self.name, 'bash', '-lc', command], timeout=timeout, check=False)
            return {'output': p.stdout + p.stderr, 'returncode': p.returncode}
        except subprocess.TimeoutExpired as e:
            return {'output': str(e), 'returncode': 124, 'timed_out': True}

    def get_template_vars(self):
        return {'working_dir': '/src', 'cwd': '/src'}

    def get_patch(self):
        result = self.execute('git add -N . && git diff --binary HEAD -- .')
        if result['returncode']:
            raise RuntimeError(result['output'])
        return result['output']

    def close(self):
        if not self.closed:
            docker(['rm', '-f', self.name], check=False)
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
