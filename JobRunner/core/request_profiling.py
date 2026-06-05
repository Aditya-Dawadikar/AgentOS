from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parent.parent
PROFILE_EVENTS_PATH = PROJECT_DIR / 'data' / 'request_profile_events.jsonl'

_PROFILE_LOCK = threading.Lock()


def append_profile_event(event: dict[str, Any]) -> Path:
    record = dict(event)
    record.setdefault('event', 'request_profile')
    record.setdefault('recorded_at', time.time())

    PROFILE_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _PROFILE_LOCK:
        with PROFILE_EVENTS_PATH.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, separators=(',', ':')))
            handle.write('\n')

    return PROFILE_EVENTS_PATH