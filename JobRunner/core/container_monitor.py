from pathlib import Path
import json
import sys
import threading
import time

import docker

from db import mark_job_completed


def _stream_docker_events(client, container_id, lifecycle_log_path, event_since, stop_event):
    events = client.events(decode=True,
                           since=event_since,
                           filters={'type': 'container', 'container': container_id})

    try:
        with lifecycle_log_path.open('a', encoding='utf-8') as lifecycle_log:
            for event in events:
                lifecycle_log.write(json.dumps(event) + '\n')
                lifecycle_log.flush()

                if stop_event.is_set():
                    break

                if event.get('Action') in {'destroy'}:
                    break
    finally:
        events.close()


def _watch_kill_switch(container, kill_switch_path):
    while True:
        if kill_switch_path.exists():
            try:
                container.kill()
            except docker.errors.NotFound:
                pass
            return

        try:
            container.reload()
        except docker.errors.NotFound:
            return

        if container.status in {'dead', 'exited'}:
            return

        time.sleep(0.5)


def main():
    container_id = sys.argv[1]
    log_path = Path(sys.argv[2])
    kill_switch_path = Path(sys.argv[3])
    lifecycle_log_path = Path(sys.argv[4])
    event_since = int(sys.argv[5])
    job_id = sys.argv[6]
    client = docker.from_env(timeout=10)
    container = None
    event_stream_stop = threading.Event()
    docker_event_stream = None
    exit_code = None
    error_message = None

    try:
        container = client.containers.get(container_id)
        docker_event_stream = threading.Thread(target=_stream_docker_events,
                                               args=(client, container_id, lifecycle_log_path, event_since, event_stream_stop),
                                               daemon=True)
        docker_event_stream.start()
        kill_switch_watcher = threading.Thread(target=_watch_kill_switch,
                                               args=(container, kill_switch_path),
                                               daemon=True)
        kill_switch_watcher.start()

        with log_path.open('ab') as log_file:
            for chunk in container.logs(stream=True,
                                        follow=True,
                                        stdout=True,
                                        stderr=True,
                                        timestamps=True):
                log_file.write(chunk)
                log_file.flush()

        wait_result = container.wait()
        exit_code = wait_result.get('StatusCode')
    except Exception as error:
        error_message = str(error)
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except docker.errors.NotFound:
                pass
            except docker.errors.APIError as error:
                error_message = str(error)
        event_stream_stop.set()
        if docker_event_stream is not None:
            docker_event_stream.join(timeout=2)
        if error_message is None and exit_code is not None and exit_code != 0:
            error_message = f'Container exited with status code {exit_code}'
        if exit_code is not None:
            mark_job_completed(job_id,
                               'SUCCEEDED' if exit_code == 0 else 'FAILED',
                               exit_code,
                               error_message)
        elif error_message is not None:
            mark_job_completed(job_id, 'FAILED', None, error_message)
        client.close()


if __name__ == '__main__':
    main()