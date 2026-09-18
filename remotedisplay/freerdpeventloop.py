# coding=utf-8
"""
FreeRdpEventLoop - pumps a FreeRDP session until told to quit.
Port of freerdpeventloop.cpp. The original collected file descriptors with
freerdp_get_fds() and select()ed on them (WaitForMultipleObjects on Windows);
FreeRDP 3 exposes WinPR event handles on every platform, so the loop is the
same on all of them:
    freerdp_get_event_handles -> WaitForMultipleObjects -> freerdp_check_event_handles
As in the C++, every iteration also runs QCoreApplication.processEvents():
the loop blocks the worker thread's Qt event loop, so without this, queued
slots on the client object (a requestStop() invoked from the GUI thread,
for instance) would never execute while a session is running.
"""
from ManyQt.QtCore import QObject, QCoreApplication
from sys import stderr, platform
from pyfreerdpnative import load
from pyfreerdpnative import types as T

MAX_HANDLES = 64
WAIT_TIMEOUT_MS = 100  # Default when nothing periodic is scheduled.
WAIT_FAILED = 0xFFFFFFFF


def _wait_function(api):
    """
    WaitForMultipleObjects. On Linux/macOS WinPR implements and exports it;
    on Windows it is the Win32 API in kernel32 (WinPR's synch.h only declares
    its own copy under #ifndef _WIN32), so asking the FreeRDP libraries for
    it there raises AttributeError - which, inside a Qt slot, PyQt5 turns
    into a process abort.
    """
    if api.has("WaitForMultipleObjects"):
        return api.WaitForMultipleObjects
    if platform == "win32":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = k32.WaitForMultipleObjects
        fn.restype = ctypes.c_uint32
        fn.argtypes = [ctypes.c_uint32, ctypes.POINTER(T.HANDLE), ctypes.c_int, ctypes.c_uint32]
        return fn
    raise RuntimeError("WaitForMultipleObjects is not available from WinPR on this platform")


class FreeRdpEventLoop(QObject):
    """
    FreeRdpEventLoop class.
    """

    def __init__(self, *args, **kwargs):
        super(FreeRdpEventLoop, self).__init__(*args, **kwargs)
        self._api = load()
        self._wait = _wait_function(self._api)
        self._context = None
        self.afterEvents = None  # callable run after each dispatch (frame capture)
        # Wake-up cadence. Frame capture happens in afterEvents, so the wait
        # timeout bounds how late a frame can be sampled when the network is
        # quiet: a 100 ms timeout means 10 fps on a quiet link and a jittery
        # 10-40 fps otherwise. Set this from the frame rate (see FreeRdpClient).
        self.wait_ms = WAIT_TIMEOUT_MS
        self._shouldQuit = False
        self._handles = (T.HANDLE * MAX_HANDLES)()

    def exec(self, context):
        """
        Run until quit() is called, the peer disconnects, or an error occurs.
        """
        self._context = context
        self._shouldQuit = False
        while not self._shouldQuit:
            if not self.handleFds():
                break
            if self.afterEvents is not None:
                try:
                    # afterEvents may return the milliseconds until it next
                    # wants to run; the wait wakes exactly then (or earlier,
                    # on network events)
                    nxt = self.afterEvents()
                    if nxt is not None:
                        # cap at 10 ms: queued input (mouse, keys) is executed by
                        # processEvents() below, so this bounds input latency
                        self.wait_ms = max(1, min(int(nxt), 10, WAIT_TIMEOUT_MS))
                except Exception:
                    import traceback
                    traceback.print_exc()
            QCoreApplication.processEvents()
        self._context = None

    exec_ = exec  # PyQt5 naming convention, as with QApplication.exec_()

    def quit(self):
        self._shouldQuit = True

    def handleFds(self):
        """
        One wait + dispatch cycle. False means the session is over.
        """
        a = self._api
        n = a.freerdp_get_event_handles(self._context, self._handles, MAX_HANDLES)
        if n == 0:
            stderr.write("Failed to get FreeRDP event handles\n")
            return False
        rc = self._wait(n, self._handles, False, int(self.wait_ms))
        if rc == WAIT_FAILED:
            stderr.write("WaitForMultipleObjects failed\n")
            return False
        if not a.freerdp_check_event_handles(self._context):
            # expected after quit()/abort or a peer disconnect; only an unexplained failure is worth a message.
            if not self._shouldQuit and not a.freerdp_shall_disconnect_context(self._context):
                stderr.write("Failed to check FreeRDP event handles\n")
            return False
        if a.freerdp_shall_disconnect_context(self._context):
            return False
        return True
