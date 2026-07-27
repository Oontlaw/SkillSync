import asyncio
import concurrent.futures
import requests
from bot_core.config import SKILLSYNC_API, API_KEY
from bot_core.logging import log

# ponytail: dedicated executor for HTTP calls so thread-pool leaks from
# Jira DNS hangs or heavy Flask endpoints can't starve flush/heartbeat.
_http_executor = concurrent.futures.ThreadPoolExecutor(max_workers=64, thread_name_prefix="http")

# ponytail: lightweight hit counter — incremented from the main thread
# before submitting to executor, decremented when future completes.
# Readable from flush_all_buffers to show thread-pool saturation.
_http_hit = 0
_http_hit_lock = __import__('threading').Lock()

try:
    from bot_core import tasks as _tasks_mod
    _inc_http = _tasks_mod._inc_http
    _dec_http = _tasks_mod._dec_http
except ImportError:
    _inc_http = _dec_http = None


def _api_post_sync(endpoint, payload):
    """Send data to SkillSync backend silently. Returns response JSON or None."""
    try:
        r = requests.post(f'{SKILLSYNC_API}{endpoint}', json=payload,
                         headers={'Authorization': f'Bearer {API_KEY}'}, timeout=5)
        if r.ok:
            return r.json()
        log(f'API {endpoint} returned {r.status_code}')
    except Exception as e:
        log(f'API error on {endpoint}: {e}')
    return None


def _api_get_sync(url, **kwargs):
    """GET request for heartbeat/health checks. Returns response or None."""
    try:
        r = requests.get(url, **kwargs)
        if r.ok:
            return r.json()
    except Exception:
        pass
    return None


async def api_post(endpoint, payload):
    """Send data to SkillSync backend silently and asynchronously. Returns response JSON or None."""
    if _inc_http: _inc_http()
    try:
        return await asyncio.get_event_loop().run_in_executor(_http_executor, _api_post_sync, endpoint, payload)
    finally:
        if _dec_http: _dec_http()


async def api_get(url, **kwargs):
    """Async GET via dedicated HTTP executor."""
    if _inc_http: _inc_http()
    try:
        return await asyncio.get_event_loop().run_in_executor(_http_executor, _api_get_sync, url, **kwargs)
    finally:
        if _dec_http: _dec_http()
