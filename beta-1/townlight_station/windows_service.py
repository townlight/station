# SPDX-License-Identifier: AGPL-3.0-only
"""Separate SCM entry point. Importing this module never registers a service.

Registration is a future explicitly authorized deployment action. Configuration
comes exclusively from the TOWNLIGHT_STATION_* environment, not CivicCast files.
"""
import subprocess
import os
from pathlib import Path
import sys
import time
import win32event
import win32service
import win32serviceutil
import servicemanager
from .boundary import IDENTITY, Settings


class StationService(win32serviceutil.ServiceFramework):
    _svc_name_ = IDENTITY['service']
    _svc_display_name_ = 'Townlight Station'
    _svc_description_ = 'Independent Townlight Station API and media job worker'

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        cfg = Settings.from_env()
        cfg.initialize_storage()
        cfg.verify_media()
        sys.path.insert(0, str(cfg.media_source))
        from civiccast.platform.child_containment import contain_own_descendants
        if not contain_own_descendants().active:
            raise RuntimeError('Station service could not contain its own children')
        # pythonservice.exe is the SCM host; run actual Python for child modules.
        python = Path(os.environ['TOWNLIGHT_STATION_CONTROL_PYTHON'])
        if not python.is_absolute() or not python.is_file() or python.name.casefold() != 'python.exe':
            raise RuntimeError('Station service requires its explicit control Python executable')
        jobs = {}
        handles = []
        attempts = {'serve': 0, 'worker': 0}
        try:
            for role in attempts:
                log = (cfg.data / f'service-{role}.log').open('a', encoding='utf-8')
                handles.append(log)
                jobs[role] = [None, log, 0.0]
            while win32event.WaitForSingleObject(self.stop_event, 500) == win32event.WAIT_TIMEOUT:
                for role, entry in jobs.items():
                    child, log, started = entry
                    if child is not None and child.poll() is None:
                        continue
                    if child is not None and time.monotonic() - started >= 60:
                        attempts[role] = 0
                    if attempts[role] >= 3:
                        continue  # Other role remains alive; operator must repair/restart.
                    attempts[role] += 1
                    if child is not None:
                        servicemanager.LogErrorMsg(f'Station {role} exited {child.returncode}; restart {attempts[role]}/3')
                    entry[0] = subprocess.Popen([str(python), '-B', '-m', 'townlight_station.cli', role],
                        stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
                    entry[2] = time.monotonic()
        finally:
            for child, _, _ in jobs.values():
                if child is not None and child.poll() is None:
                    child.terminate()
                    child.wait(timeout=15)
            for log in handles:
                log.close()


if __name__ == '__main__':
    win32serviceutil.HandleCommandLine(StationService)
