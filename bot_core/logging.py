import os
from datetime import datetime

LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'skillsync_bot.log')

_MAX_LOG_BYTES = 32 * 1024 * 1024
_ROTATE_CHECK_EVERY = 2000
_write_count = 0


def _rotate_if_large():
    """Rotate the log once it exceeds _MAX_LOG_BYTES (checked every N writes)."""
    global _write_count
    _write_count += 1
    if _write_count % _ROTATE_CHECK_EVERY and _write_count > 1:
        return
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > _MAX_LOG_BYTES:
            os.replace(LOG_FILE, f'{LOG_FILE}.{datetime.now().strftime("%Y%m%d-%H%M%S")}')
    except Exception:
        pass


_rotate_if_large()  # rotate an oversized log at process start


def log(msg):
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}\n')
    except Exception as e:
        print(f'[SkillSync] Log write failed: {e}')
    _rotate_if_large()
