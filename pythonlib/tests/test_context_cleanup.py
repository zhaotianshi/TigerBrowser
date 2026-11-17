"""Already-closed targets remain idempotent; real teardown failures still propagate."""

import asyncio
from types import SimpleNamespace

import pytest
from playwright._impl._errors import TargetClosedError

from camoufox import async_api, sync_api


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('closed', ['open', 'closed', 'failed'])
@pytest.mark.parametrize('driver_failed', [False, True])
def test_context_exit_distinguishes_closed_target_from_cleanup_failure(
        monkeypatch, asynchronous, closed, driver_failed):
    events = []
    close_error = {'open': None, 'closed': TargetClosedError(),
                   'failed': RuntimeError('browser cleanup failed')}[closed]
    driver_error = RuntimeError('driver cleanup failed') if driver_failed else None

    def close():
        events.append('browser')
        if close_error is not None:
            raise close_error

    def exit(*args):
        events.append('driver')
        if driver_error is not None:
            raise driver_error

    if asynchronous:
        async def async_close():
            close()

        async def async_exit(*args):
            exit(*args)

        monkeypatch.setattr(async_api.PlaywrightContextManager, '__aexit__', async_exit)
        manager = async_api.AsyncCamoufox()
        manager.browser = SimpleNamespace(close=async_close)
        invoke = lambda: asyncio.run(manager.__aexit__(None, None, None))
    else:
        monkeypatch.setattr(sync_api.PlaywrightContextManager, '__exit__', exit)
        manager = sync_api.Camoufox()
        manager.browser = SimpleNamespace(close=close)
        invoke = lambda: manager.__exit__(None, None, None)

    expected = driver_error or (close_error if closed == 'failed' else None)
    if expected is None:
        invoke()
    else:
        with pytest.raises(RuntimeError) as raised:
            invoke()
        assert raised.value is expected
    assert events == ['browser', 'driver']
