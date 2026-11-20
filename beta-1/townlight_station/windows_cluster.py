# SPDX-License-Identifier: AGPL-3.0-only
"""Give initdb its bootstrap password through an account-private memory pipe."""
import secrets
import subprocess


def initialize_cluster(pg, cluster, password):
    import pywintypes
    import win32api
    import win32event
    import win32file
    import win32pipe
    import win32security
    sid = win32security.ConvertSidToStringSid(win32security.LookupAccountName(None, win32api.GetUserNameEx(2))[0])
    security = pywintypes.SECURITY_ATTRIBUTES()
    security.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor('D:P(A;;GA;;;SY)(A;;GA;;;'+sid+')', 1)
    name = r'\\.\pipe\TownlightStation-'+secrets.token_hex(24)
    pipe = win32pipe.CreateNamedPipe(name, win32pipe.PIPE_ACCESS_OUTBOUND | win32file.FILE_FLAG_OVERLAPPED,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_WAIT | 8, 1, 4096, 4096, 180000, security)
    child = None
    event = win32event.CreateEvent(None, True, False, None)
    try:
        pending = pywintypes.OVERLAPPED(); pending.hEvent = event
        try:
            win32pipe.ConnectNamedPipe(pipe, pending)
        except pywintypes.error as exc:
            if exc.winerror not in (997, 535):
                raise
            if exc.winerror == 535:
                win32event.SetEvent(event)
        child = subprocess.Popen([str(pg/'initdb.exe'), '-D', str(cluster), '-U', 'station_owner',
            '--auth=scram-sha-256', '--encoding=UTF8', '--pwfile='+name], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        if win32event.WaitForSingleObject(event, 180000) != win32event.WAIT_OBJECT_0:
            raise RuntimeError('Recorder database did not open its bootstrap pipe')
        write = pywintypes.OVERLAPPED()
        win32file.WriteFile(pipe, (password+'\n').encode('utf-8'), write)
        win32file.GetOverlappedResult(pipe, write, True)
        stdout, stderr = child.communicate(timeout=180)
        if child.returncode:
            raise RuntimeError('Recorder database initialization failed: '+stderr.decode('utf-8', errors='replace')[-1000:])
    finally:
        if child is not None and child.poll() is None:
            child.kill(); child.communicate()
        win32file.CloseHandle(pipe)
        win32file.CloseHandle(event)
