# SPDX-License-Identifier: AGPL-3.0-only
import argparse
from uuid import UUID
from .boundary import Settings
from .store import Store


def main():
    parser = argparse.ArgumentParser(prog='townlight-station')
    parser.add_argument('command', choices=['migrate', 'serve', 'worker', 'air', 'operator', 'uploads', 'setup', 'background', 'backup', 'restore', 'stop'])
    parser.add_argument('--port', type=int, default=8756)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--profile-root', type=str)
    parser.add_argument('--config', type=str)
    parser.add_argument('--payload-root', type=str)
    parser.add_argument('--data-root', type=str)
    parser.add_argument('--destination', type=str)
    parser.add_argument('--suite-url', type=str, default='http://127.0.0.1:15480')
    parser.add_argument('--meeting-id', type=UUID)
    args = parser.parse_args()
    if args.meeting_id and args.command != 'operator':
        parser.error('--meeting-id is only valid with operator')
    if args.command == 'setup':
        import json
        from .setup_windows import provision
        if not args.payload_root or not args.data_root:
            parser.error('setup requires explicit payload and data directories')
        print(json.dumps(provision(args.payload_root, args.data_root, args.suite_url)))
        return
    if args.command == 'background':
        from .setup_windows import background
        if not args.config:
            parser.error('background requires the protected configuration path')
        # pythonw has no console: keep failures in the protected recorder directory.
        from pathlib import Path
        from contextlib import redirect_stdout, redirect_stderr
        from .boundary import private_path
        from .setup_windows import load_configuration
        import traceback
        load_configuration(args.config)
        log_path = private_path(Path(args.config).parent, 'background.log')
        with log_path.open('a', encoding='utf-8') as log, redirect_stdout(log), redirect_stderr(log):
            try:
                background(args.config)
            except Exception:
                traceback.print_exc(file=log)
                raise
        return
    if args.config:
        from .setup_windows import load_configuration
        load_configuration(args.config)
    if args.command == 'stop':
        import json
        from .setup_windows import stop_task
        if not args.config:
            parser.error('stop requires the recorder configuration')
        print(json.dumps(stop_task(args.config)))
        return
    cfg = Settings.from_env() if args.command != 'operator' else None
    if args.command in ('backup', 'restore'):
        import json
        from .backup import backup, restore
        if not args.destination:
            parser.error('backup/restore requires the selected absolute directory')
        print(json.dumps((backup if args.command == 'backup' else restore)(cfg, args.destination)))
    elif args.command == 'migrate':
        cfg.initialize_storage()
        store = Store(cfg.database_url)
        try:
            store.migrate()
        finally:
            store.engine.dispose()
    elif args.command == 'serve':
        import uvicorn
        from .api import create_app
        uvicorn.run(create_app(cfg), host='127.0.0.1', port=args.port, access_log=False)
    elif args.command == 'uploads':
        from .delivery import run_uploads
        run_uploads(once=args.once)
    elif args.command == 'air':
        # The on-air program itself: a real emitted output kept in step with the
        # durable on-air decision the serve/worker processes write.
        from .airplay import run_air
        run_air()
    elif args.command == 'operator':
        import json
        from pathlib import Path
        import httpx
        import webbrowser
        if not args.profile_root or not Path(args.profile_root).is_absolute():
            parser.error('operator requires the explicit absolute Townlight --profile-root')
        # Deliberate local staff launch, never an unauthenticated HTTP read of
        # the native proof. Only a single-use 60-second ticket reaches a browser.
        proof = json.loads((Path(args.profile_root)/'config/local-session.json').read_text())['session_hash']
        origin = f'http://127.0.0.1:{args.port}'
        with httpx.Client(trust_env=False, timeout=15) as client:
            result = client.post(origin+'/v1/operator-ticket', headers={'X-Townlight-Session': proof})
            result.raise_for_status()
        from urllib.parse import urlencode
        url = origin+'/#ticket='+result.json()['ticket']
        if args.meeting_id:
            url += '&'+urlencode({'meeting': str(args.meeting_id)})
        import os
        if os.environ.get('TOWNLIGHT_SUPPRESS_OPEN_BROWSER') == '1':
            print(json.dumps({'operator_url': url}))
        else:
            webbrowser.open(url)
    else:
        from .worker import run
        run(once=args.once)


if __name__ == '__main__':
    main()
