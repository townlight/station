# SPDX-License-Identifier: AGPL-3.0-only
import argparse
from .boundary import Settings
from .store import Store


def main():
    parser = argparse.ArgumentParser(prog='townlight-station')
    parser.add_argument('command', choices=['migrate', 'serve', 'worker', 'air', 'operator', 'uploads'])
    parser.add_argument('--port', type=int, default=8756)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--profile-root', type=str)
    args = parser.parse_args()
    cfg = Settings.from_env() if args.command != 'operator' else None
    if args.command == 'migrate':
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
        webbrowser.open(origin+'/#ticket='+result.json()['ticket'])
    else:
        from .worker import run
        run(once=args.once)


if __name__ == '__main__':
    main()
