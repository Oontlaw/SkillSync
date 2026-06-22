import asyncio
import requests
from bot_core.config import SKILLSYNC_API, API_KEY
from bot_core.logging import log


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


async def api_post(endpoint, payload):
    """Send data to SkillSync backend silently and asynchronously. Returns response JSON or None."""
    return await asyncio.to_thread(_api_post_sync, endpoint, payload)
