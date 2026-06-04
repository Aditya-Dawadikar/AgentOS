from pathlib import Path
import io
import json
import os
import subprocess
import sys
import tarfile
import time
from uuid import uuid4

import docker

from db import upsert_job, update_job

client = docker.from_env(timeout=10)
project_dir = Path(__file__).resolve().parent.parent
data_root = project_dir / 'data'
jobs_root = project_dir / 'jobs'


def _start_container_monitor(container_id, log_path, kill_switch_path, lifecycle_log_path, event_since, job_id):
    monitor_executable = sys.executable
    monitor_command = [monitor_executable,
                       '-m',
                       'core.container_monitor',
                       container_id,
                       str(log_path),
                       str(kill_switch_path),
                       str(lifecycle_log_path),
                       str(event_since),
                       job_id]

    if os.name == 'nt':
        pythonw_executable = str(Path(sys.executable).with_name('pythonw.exe'))
        if Path(pythonw_executable).exists():
            monitor_command[0] = pythonw_executable

    popen_kwargs = {
        'cwd': str(project_dir),
        'stdout': subprocess.DEVNULL,
        'stderr': subprocess.DEVNULL,
        'close_fds': True,
    }

    if os.name == 'nt':
        popen_kwargs['creationflags'] = (getattr(subprocess, 'DETACHED_PROCESS', 0)
                                         | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
                                         | getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= getattr(subprocess, 'STARTF_USESHOWWINDOW', 0)
        startupinfo.wShowWindow = getattr(subprocess, 'SW_HIDE', 0)
        popen_kwargs['startupinfo'] = startupinfo
    else:
        popen_kwargs['start_new_session'] = True

    subprocess.Popen(monitor_command, **popen_kwargs)


def _build_job_archive(job_dir):
    archive_buffer = io.BytesIO()

    with tarfile.open(fileobj=archive_buffer, mode='w') as archive:
        app_dir_info = tarfile.TarInfo(name='app')
        app_dir_info.type = tarfile.DIRTYPE
        app_dir_info.mode = 0o755
        archive.addfile(app_dir_info)

        for path in sorted(job_dir.rglob('*')):
            archive.add(path, arcname=Path('app') / path.relative_to(job_dir))

    archive_buffer.seek(0)
    return archive_buffer.getvalue()


def run_job(job_id):
    job_dir = jobs_root / job_id
    if not job_dir.is_dir():
        raise FileNotFoundError(f'Job directory not found: {job_dir}')

    container_name = f'agent_os_job_{str(uuid4())}'
    container_data_dir = data_root / job_id
    container_data_dir.mkdir(parents=True, exist_ok=False)
    container_desc_path = container_data_dir / 'container_desc.txt'
    kill_switch_path = container_data_dir / 'kill_switch.txt'
    lifecycle_log_path = container_data_dir / 'container_lifecycle.txt'
    log_path = container_data_dir / 'container_logs.txt'
    event_since = int(time.time()) - 1

    upsert_job(job_id, str(job_dir), 'STARTING')

    try:
        container = client.containers.create(name=container_name,
                                             image='python:3-alpine',
                                             command=['python', '/app/job.py'],
                                             nano_cpus=500_000_000,
                                             mem_limit='128m',
                                             memswap_limit='128m',
                                             pids_limit=64,
                                             volumes={str(container_data_dir): {'bind': '/data', 'mode': 'rw'}},
                                             healthcheck={'test': ['CMD', 'echo', 'hello from container']})

        container.put_archive('/', _build_job_archive(job_dir))

        _start_container_monitor(container.id, log_path, kill_switch_path, lifecycle_log_path, event_since, job_id)
        container.start()

        container_desc = {
            'job_id': job_id,
            'job_dir': str(job_dir),
            'container_name': container_name,
            'container_id': container.id,
            'data_dir': str(container_data_dir),
            'kill_switch_path': str(kill_switch_path),
            'lifecycle_log_path': str(lifecycle_log_path),
            'log_path': str(log_path),
        }
        container_desc_path.write_text(json.dumps(container_desc, indent=2), encoding='utf-8')
        update_job(job_id,
                   status='RUNNING',
                   container_id=container.id,
                   container_name=container_name,
                   data_dir=str(container_data_dir),
                   kill_switch_path=str(kill_switch_path),
                   lifecycle_log_path=str(lifecycle_log_path),
                   log_path=str(log_path),
                   error_message=None)

        return container_desc
    except Exception as error:
        update_job(job_id,
                   status='FAILED',
                   data_dir=str(container_data_dir),
                   kill_switch_path=str(kill_switch_path),
                   lifecycle_log_path=str(lifecycle_log_path),
                   log_path=str(log_path),
                   error_message=str(error),
                   completed_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        raise


if __name__ == '__main__':
    print(run_job(sys.argv[1]))