"""HTTP-by-default launch configuration for the single-worker team service."""

from urllib.parse import urlsplit

from .service import create_app
from .native import MAX_INPUT_MESSAGE_BYTES


def configuration(root, *, origin='http://127.0.0.1:8766', listen='127.0.0.1', cert=None, key=None,
                  browser_mode='disabled', private_network_only=False, allow_client=(),
                  execution_boundary=None,
                  automation_listen=False, automation_port=8767, kernel_root=None, staging_root=None,
                  client_root=None):
    import uvicorn

    parsed = urlsplit(origin)
    if parsed.scheme == 'http' and (cert or key):
        raise ValueError('An HTTP origin cannot use TLS certificate files.')
    if parsed.scheme == 'https' and (not cert or not key):
        raise ValueError('An HTTPS origin requires both a certificate and private key.')
    if (type(automation_port) is not int or not 1 <= automation_port <= 65535
            or automation_listen and automation_port == (parsed.port or (443 if parsed.scheme == 'https' else 80))):
        raise ValueError('Use a separate automation port between 1 and 65535.')
    app = create_app(
        root, origin=origin, private_network_only=private_network_only, allow_client=allow_client,
        browser_mode=browser_mode, execution_boundary=execution_boundary, kernel_root=kernel_root, staging_root=staging_root,
        client_root=client_root,
        automation=({'host': listen, 'port': automation_port, 'origin': origin,
                     'cert': str(cert) if cert else None, 'key': str(key) if key else None}
                    if automation_listen else None),
    )
    return uvicorn.Config(
        app, host=listen, port=parsed.port or (443 if parsed.scheme == 'https' else 80),
        ssl_certfile=str(cert) if cert else None, ssl_keyfile=str(key) if key else None,
        workers=1, reload=False, proxy_headers=False, access_log=False, server_header=False,
        limit_concurrency=64, backlog=64, timeout_keep_alive=5, timeout_graceful_shutdown=None,
        ws='websockets', ws_max_size=MAX_INPUT_MESSAGE_BYTES, ws_max_queue=2, ws_per_message_deflate=False,
        log_level='warning',
    )


def _boundary_arguments(root, options):
    arguments = ['studio-team', '--data-dir', str(root)]
    for key, value in options.items():
        option = '--' + key.replace('_', '-')
        if value is True:
            arguments.append(option)
        elif isinstance(value, (list, tuple)):
            for item in value:
                arguments += [option, str(item)]
        elif value is not None and value is not False:
            arguments += [option, str(value)]
    return arguments


def run(root, **kwargs):
    import uvicorn
    from .boundary import ensure

    # Validate configuration before creating a kernel boundary or taking ownership.
    configuration(root, **kwargs)
    arguments = _boundary_arguments(root, kwargs)
    kwargs['execution_boundary'] = ensure(arguments)
    config = configuration(root, **kwargs)
    origin = kwargs.get('origin', 'http://127.0.0.1:8766')
    if urlsplit(origin).scheme == 'http':
        scope = ' Direct peers are restricted to private addresses.' if kwargs.get('private_network_only') else ''
        allowlist = ' A client IP allowlist is active.' if kwargs.get('allow_client') else ''
        print('WARNING: HTTP sends passwords, browser frames and files without encryption.'
              + scope + allowlist, flush=True)
    print('Team service starting in explicit ' + kwargs.get('browser_mode', 'disabled') + ' mode.', flush=True)
    uvicorn.Server(config).run()
