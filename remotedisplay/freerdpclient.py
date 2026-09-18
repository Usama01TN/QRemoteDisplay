# coding=utf-8
"""
FreeRdpClient - the FreeRDP session, living on a worker thread.
Port of freerdpclient.cpp + freerdpeventloop.cpp to FreeRDP 3 through
pyfreerdpnative. The FreeRDP 1.x calls the original used have these
counterparts:
    freerdp_new + freerdp_context_new     -> freerdp_client_context_new
    freerdp_channels_* / load_addins      -> done by freerdp_connect
    update->BitmapUpdate (raw 16bpp)      -> gdi_init: software GDI paints every
                                             codec into a BGRX32 framebuffer;
                                             update->EndPaint tells us when
    pointer_cache_register_callbacks +
    graphics_register_pointer(rdpPointer) -> same, via the generated rdpPointer
    freerdp_get_fds / check_fds loop      -> freerdp_get_event_handles /
                                             WaitForMultipleObjects /
                                             freerdp_check_event_handles
    freerdp_keyboard_get_rdp_scancode_from_x11_keycode -> same (X11)
Every callback handed to C is kept in self._refs: ctypes does not keep them
alive, and a collected callback is a crash on the next call from C.
"""
import ctypes
import os
import sys
import time
import traceback
from pyfreerdpnative import load
from pyfreerdpnative import types as T
from pyfreerdpnative.freerdp import client as CLIENT
from pyfreerdpnative.freerdp import freerdp as F
from pyfreerdpnative.freerdp import input as INPUT
from pyfreerdpnative.winpr import input as WINPR_INPUT
from pyfreerdpnative.freerdp import settings_keys as KEY
from pyfreerdpnative.freerdp.channels import geometry as CH_GEOMETRY
from pyfreerdpnative.freerdp.channels import rdpgfx as CH_RDPGFX
from pyfreerdpnative.freerdp.channels import video as CH_VIDEO
from pyfreerdpnative.freerdp.codec import color as COLOR
from .freerdpeventloop import FreeRdpEventLoop
from ManyQt.QtCore import Qt, Signal, QSize, Slot, QEvent, QRect, QObject

_api = None


def _flag(name, default):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "no", "off", "false", "")


FRAMERATE_LIMIT = 40  # frames per second handed to the GUI, as in the C++
DEBUG_CALLBACKS = bool(os.environ.get("REMOTEDISPLAY_DEBUG"))


def guard(default=False):
    """
    Wrap a method used as a C callback.
    FreeRDP calls these from freerdp_check_event_handles(); a Python
    exception escaping into C aborts the process (and the traceback is lost),
    which looks like a crash inside check_event_handles. Catch everything,
    report it once, and return a safe value instead.
    """

    def decorate(method):
        name = method.__name__

        def wrapper(self, *args):
            if DEBUG_CALLBACKS:
                sys.stderr.write("[remotedisplay] -> {0}\n".format(name))
                sys.stderr.flush()
            try:
                return method(self, *args)
            except Exception:
                if name not in self._reported:
                    self._reported.add(name)
                    sys.stderr.write("[remotedisplay] exception in {0}:\n{1}".format(
                        name, traceback.format_exc()))
                    sys.stderr.flush()
                return default

        wrapper.__name__ = name
        return wrapper

    return decorate


def api():
    """The loaded, prototype-bound FreeRDP libraries (shared by all clients)."""
    global _api
    if _api is None:
        _api = load()
    return _api


def _qt_button_to_rdp(button):
    if button == Qt.LeftButton:
        return INPUT.PTR_FLAGS_BUTTON1
    if button == Qt.RightButton:
        return INPUT.PTR_FLAGS_BUTTON2
    if button == Qt.MiddleButton:
        return INPUT.PTR_FLAGS_BUTTON3
    return 0


class FreeRdpClient(QObject):
    """
    FreeRdpClient class.
    """
    aboutToConnect = Signal()
    connected = Signal()
    disconnected = Signal()
    desktopUpdated = Signal(QRect)
    # (rect, BGRX32 pixels of that rect, bytes per row, desktop w, desktop h):
    # the pixels are copied on FreeRDP's thread while the GDI buffer is valid
    desktopRectangle = Signal(QRect, bytes, int, int, int)
    # one emission per captured frame: [(rect, pixels), ...], stride, w, h -
    # a single queued event and a single repaint however many bands changed
    desktopFrame = Signal(list, int, int, int)
    connectionFailed = Signal(str)
    channelConnected = Signal(str)
    # cross-thread requests from the widget (queued, since we live on a worker thread)
    _runRequested = Signal()
    # Input MUST be sent on FreeRDP's thread: freerdp_input_send_* writes to
    # the transport, and a concurrent write from the GUI thread while the
    # RDP thread is inside check_event_handles corrupts it (access violation
    # under load - the Windows key, which floods the screen with updates the
    # moment it is pressed, is a reliable trigger). The C++ used
    # QMetaObject::invokeMethod(QueuedConnection) for the same reason; these
    # queued signals are its equivalent, executed by processEvents() in the
    # event loop.
    _mouseRequested = Signal(int, int, int)
    _keyRequested = Signal(bool, int, int, int)  # down, native_scancode, native_vkey, unicode

    def __init__(self, pointer_sink, parent=None):
        QObject.__init__(self, parent)
        self._api = api()
        self._pointer_sink = pointer_sink
        self._ctx = None  # POINTER(rdpContext)
        self._instance = None  # POINTER(freerdp)
        self._gdi = None  # POINTER(rdpGdi)
        self._refs = []  # callbacks handed to C - MUST stay alive
        self._reported = set()  # callbacks whose failure was already logged
        self.loop = FreeRdpEventLoop(self)
        self.loop.afterEvents = self._capture_frame
        self.loop.wait_ms = max(1, 1000 // FRAMERATE_LIMIT)  # refined per iteration
        self._host, self._port = "", 3389
        self._size = QSize(1024, 768)
        self._user, self._password, self._domain = "", "", ""
        # Feature switches. Each one removes a place where FreeRDP calls back
        # into Python, so a native crash on one platform can be bisected
        # without touching code: REMOTEDISPLAY_NO_CURSOR / _NO_GFX / _NO_SOUND.
        # Defaults are the SAFE configuration: FreeRDP's own audio backend
        # (no Qt device plugin), no pointer callbacks (Qt shows the local
        # cursor), Graphics Pipeline on but without H.264 - the H.264 decoders
        # are the [experimental] part of a media build and the first frames
        # after logon go straight through them on a Windows host. Each can be
        # enabled explicitly once the plain session is proven stable.
        self.use_qt_sound = _flag("REMOTEDISPLAY_QT_SOUND", False)
        self.remote_cursor = _flag("REMOTEDISPLAY_REMOTE_CURSOR", True)
        self.graphics_pipeline = _flag("REMOTEDISPLAY_GFX", False)
        self.h264 = _flag("REMOTEDISPLAY_H264", False)
        self.clipboard = _flag("REMOTEDISPLAY_CLIPBOARD", True)
        self.clipboardBridge = None  # set by the widget (needs the GUI thread)
        # (audio: FreeRDP's own default applies unless the Qt plugin is enabled)
        # The cross-thread slots are connected in _threadStarted(), which
        # runs ON the worker thread (from QThread.started). A queued
        # connection delivers to the thread the receiver lived on WHEN
        # CONNECTED; connecting here - on the GUI thread, before the widget's
        # moveToThread() - would target the GUI thread, so run() and every
        # input slot would execute on the GUI thread and race the session.
        self._threadReady = False
        self._pendingStart = False

    # --- configuration (any thread; read once by _run before connecting) --------
    def configure(self, host, port, width, height, user="", password="", domain=""):
        self._host, self._port = host, int(port)
        self._size = QSize(int(width), int(height))
        self._user, self._password, self._domain = user or "", password or "", domain or ""

    def start(self):
        self._pendingStart = True
        if self._threadReady:
            self._runRequested.emit()

    @Slot()
    def _threadStarted(self):
        """On the worker thread (QThread.started). Wire the queued slots here
        so their affinity is this thread, then start the session if
        connectToHost() already asked for it."""
        Q = Qt.QueuedConnection
        self._mouseRequested.connect(self._sendMouseOnRdpThread, Q)
        self._keyRequested.connect(self._sendKeyOnRdpThread, Q)
        self._runRequested.connect(self.run, Q)
        self._threadReady = True
        if self._pendingStart:
            self._runRequested.emit()

    # --- FreeRDP callbacks (FreeRDP thread) -------------------------------------
    @guard(None)
    def _channel_connected(self, context, e):
        name = e.contents.name
        iface = e.contents.pInterface
        a = self._api
        if not iface:
            return
        if name == b"cliprdr":
            if self.clipboardBridge is not None:
                self.clipboardBridge.attach(iface)
            self.channelConnected.emit("cliprdr")
            return
        if not self._gdi:
            return
        if name == CH_RDPGFX.RDPGFX_DVC_CHANNEL_NAME.encode():
            a.gdi_graphics_pipeline_init(self._gdi, ctypes.cast(iface, ctypes.POINTER(T.RdpgfxClientContext)))
        elif name == CH_GEOMETRY.GEOMETRY_DVC_CHANNEL_NAME.encode():
            a.gdi_video_geometry_init(self._gdi, ctypes.cast(iface, ctypes.POINTER(T.GeometryClientContext)))
        elif name == CH_VIDEO.VIDEO_CONTROL_DVC_CHANNEL_NAME.encode():
            a.gdi_video_control_init(self._gdi, ctypes.cast(iface, ctypes.POINTER(T.VideoClientContext)))
        elif name == CH_VIDEO.VIDEO_DATA_DVC_CHANNEL_NAME.encode():
            a.gdi_video_data_init(self._gdi, ctypes.cast(iface, ctypes.POINTER(T.VideoClientContext)))
        self.channelConnected.emit(name.decode(errors="replace"))

    @guard(None)
    def _channel_disconnected(self, context, e):
        name = e.contents.name
        iface = e.contents.pInterface
        a = self._api
        if name == b"cliprdr" and self.clipboardBridge is not None:
            self.clipboardBridge.detach()
            return
        if not self._gdi or not iface:
            return
        if name == CH_RDPGFX.RDPGFX_DVC_CHANNEL_NAME.encode():
            a.gdi_graphics_pipeline_uninit(self._gdi, ctypes.cast(iface, ctypes.POINTER(T.RdpgfxClientContext)))
        elif name == CH_GEOMETRY.GEOMETRY_DVC_CHANNEL_NAME.encode():
            a.gdi_video_geometry_uninit(self._gdi, ctypes.cast(iface, ctypes.POINTER(T.GeometryClientContext)))
        elif name == CH_VIDEO.VIDEO_CONTROL_DVC_CHANNEL_NAME.encode():
            a.gdi_video_control_uninit(self._gdi, ctypes.cast(iface, ctypes.POINTER(T.VideoClientContext)))
        elif name == CH_VIDEO.VIDEO_DATA_DVC_CHANNEL_NAME.encode():
            a.gdi_video_data_uninit(self._gdi, ctypes.cast(iface, ctypes.POINTER(T.VideoClientContext)))

    # --- frame capture (RDP thread, between event-loop iterations) -----------------
    BAND_ROWS = 16  # compare the framebuffer in bands of this many rows

    def _capture_frame(self):
        """
        Called by FreeRdpEventLoop after every freerdp_check_event_handles(),
        on FreeRDP's own thread, so the GDI buffer cannot change under us.

        Cost per 1080p frame, measured: memmove into a preallocated buffer
        0.7 ms, whole-frame memcmp 0.7 ms, 68 band compares 2.3 ms. Two
        things must be avoided here: allocating the frame buffer each time
        (string_at: ~30 ms of page faults) and comparing memoryviews of
        bytes (CPython compares those element by element: ~500 ms).
        """
        if not self._gdi:
            return 1000.0 / FRAMERATE_LIMIT
        now = time.time()
        interval = 1.0 / FRAMERATE_LIMIT
        remaining = interval - (now - self._last_frame_time)
        if remaining > 0.0005:
            return remaining * 1000.0  # wake again exactly at the deadline
        g = self._gdi.contents
        w, h, stride, buf = int(g.width), int(g.height), int(g.stride), g.primary_buffer
        if not buf or w <= 0 or h <= 0 or stride < w * 4:
            return 1000.0 / FRAMERATE_LIMIT
        size = stride * h
        self._last_frame_time = now  # strict minimum spacing; no catch-up bursts

        if self._cur is None or len(self._cur) != size:  # first frame / resize
            self._cur, self._prev = bytearray(size), None
            self._geom = (w, h, stride)
        cur = self._cur
        ctypes.memmove((ctypes.c_char * size).from_buffer(cur), ctypes.addressof(buf.contents), size)
        prev = self._prev
        bands = []
        if prev is None:
            bands.append((QRect(0, 0, w, h), bytes(cur)))
        elif cur != prev:  # memcmp
            band = self.BAND_ROWS * stride
            mc, mp = memoryview(cur).cast("Q"), memoryview(prev).cast("Q")
            q = band // 8
            dirty = None
            for i, lo in enumerate(range(0, size, band)):
                hi = min(lo + band, size)
                if mc[lo // 8:hi // 8] != mp[lo // 8:hi // 8]:
                    y = i * self.BAND_ROWS
                    r = QRect(0, y, w, min(self.BAND_ROWS, h - y))
                    if dirty is not None and dirty.bottom() + 1 == y:
                        dirty = dirty.united(r)  # merge adjacent bands
                    else:
                        if dirty is not None:
                            bands.append((dirty, bytes(cur[dirty.y() * stride:(dirty.y() + dirty.height()) * stride])))
                        dirty = r
            if dirty is not None:
                bands.append((dirty, bytes(cur[dirty.y() * stride:(dirty.y() + dirty.height()) * stride])))
        # the current frame becomes the reference; reuse the old buffer next time
        self._cur, self._prev = (prev if prev is not None else bytearray(size)), cur
        if bands:
            self.desktopFrame.emit(bands, stride, w, h)
            self.desktopUpdated.emit(bands[0][0] if len(bands) == 1 else QRect(0, 0, w, h))
        return 1000.0 / FRAMERATE_LIMIT

    def requestFullFrame(self):
        """Make the next capture send the whole desktop (after a view resize)."""
        self._prev = None

    @guard(False)
    def _pointer_new(self, context, pointer):
        return bool(self._pointer_sink.addPointer(pointer))

    @guard(None)
    def _pointer_free(self, context, pointer):
        self._pointer_sink.removePointer(pointer)

    @guard(False)
    def _pointer_set(self, context, pointer):
        return bool(self._pointer_sink.changePointer(pointer))

    @guard(True)
    def _pointer_set_null(self, context):
        self._pointer_sink.setNull()  # server hides the cursor
        return True

    @guard(True)
    def _pointer_set_default(self, context):
        self._pointer_sink.setDefault()  # back to the system arrow
        return True

    @guard(True)
    def _pointer_set_position(self, context, x, y):
        return True

    # --- input: GUI thread side (just computes values and queues them) --------------
    def sendMouseMoveEvent(self, pos):
        self._mouseRequested.emit(INPUT.PTR_FLAGS_MOVE, pos.x(), pos.y())

    def sendMousePressEvent(self, button, pos):
        b = _qt_button_to_rdp(button)
        if b:
            self._mouseRequested.emit(b | INPUT.PTR_FLAGS_DOWN, pos.x(), pos.y())

    def sendMouseReleaseEvent(self, button, pos):
        b = _qt_button_to_rdp(button)
        if b:
            self._mouseRequested.emit(b, pos.x(), pos.y())

    def sendWheelEvent(self, delta_y):
        # WM_MOUSEWHEEL semantics: 9-bit signed rotation in the flags
        flags = INPUT.PTR_FLAGS_WHEEL
        step = max(-255, min(255, int(delta_y)))
        if step < 0:
            flags |= INPUT.PTR_FLAGS_WHEEL_NEGATIVE
            step = -step
        self._mouseRequested.emit(flags | (step & INPUT.WheelRotationMask), 0, 0)

    def sendKeyEvent(self, event):
        """
        Read the plain integers out of the QKeyEvent here (Qt frees the event
        after this returns) and queue them. Crucially, NOTHING touches the
        FreeRDP library on the GUI thread - not even the scancode lookup,
        which calls into WinPR; doing that concurrently with the RDP thread
        was the remaining crash. The mapping happens on the RDP thread.
        """
        if event.isAutoRepeat():
            return
        down = event.type() == QEvent.KeyPress
        native = int(event.nativeScanCode())
        vkey = int(event.nativeVirtualKey())
        text = event.text()
        uni = ord(text) if (len(text) == 1 and ord(text) >= 32) else 0
        self._keyRequested.emit(down, native, vkey, uni)

    def _map_scancode(self, native, vkey):
        """native keycode -> RDP scancode, on the RDP thread (calls WinPR)."""
        if native == 0 and vkey == 0:
            return None
        if sys.platform.startswith("linux"):
            # Modern X servers (and Qt on Wayland) report evdev keycodes + 8.
            # WinPR's evdev table maps those deterministically; FreeRDP's own
            # xkb-derived table is only a fallback, because it needs a live
            # X connection to be filled in correctly.
            if native > 8:
                vk = self._api.GetVirtualKeyCodeFromKeycode(native - 8, WINPR_INPUT.WINPR_KEYCODE_TYPE_EVDEV)
                if vk:
                    sc = self._api.GetVirtualScanCodeFromVirtualKeyCode(vk, WINPR_INPUT.WINPR_KBD_TYPE_IBM_ENHANCED)
                    if sc:
                        return sc
            code = self._api.freerdp_keyboard_get_rdp_scancode_from_x11_keycode(native)
            return code if code and code != native else None
        if sys.platform == "win32":
            # Qt gives the hardware scancode; bit 8 marks the extended (E0)
            # keys - Win, right Ctrl/Alt, arrows, Insert..PageDown, numpad /
            code = native & 0xFF
            if not code:
                return None
            return code | (WINPR_INPUT.KBDEXT if native & 0x100 else 0)  # bit 8 = extended (E0)
        # macOS: nativeVirtualKey() is the Carbon/Apple keycode. winpr maps it
        # to a Windows virtual key, then to the scancode of the IBM enhanced
        # (101/102-key) layout, which is what RDP transmits.
        vk = self._api.GetVirtualKeyCodeFromKeycode(vkey,
                                                    WINPR_INPUT.WINPR_KEYCODE_TYPE_APPLE)
        if not vk:
            return None
        sc = self._api.GetVirtualScanCodeFromVirtualKeyCode(
            vk, WINPR_INPUT.WINPR_KBD_TYPE_IBM_ENHANCED)
        return sc or None

    # --- input: RDP thread side ---------------------------------------------------------
    def _input(self):
        if self._ctx is None:
            return None
        inp = self._ctx.contents.input
        return inp if inp else None

    @guard(None)
    @Slot(int, int, int)
    def _sendMouseOnRdpThread(self, flags, x, y):
        inp = self._input()
        if inp:
            self._api.freerdp_input_send_mouse_event(inp, flags, x, y)

    @guard(None)
    @Slot(bool, int, int, int)
    def _sendKeyOnRdpThread(self, down, native, vkey, uni):
        inp = self._input()
        if not inp:
            return
        code = self._map_scancode(native, vkey)
        if code is not None:
            self._api.freerdp_input_send_keyboard_event_ex(inp, bool(down), False, code)
        elif uni:
            flags = 0 if down else INPUT.KBD_FLAGS_RELEASE
            self._api.freerdp_input_send_unicode_keyboard_event(inp, flags, uni)

    # --- session (worker thread) -----------------------------------------------
    @Slot()
    def requestStop(self):
        self.loop.quit()
        if self._ctx:  # also unblocks a wait in progress
            self._api.freerdp_abort_connect_context(self._ctx)

    @Slot()
    def run(self):
        # A Python exception escaping a Qt slot is fatal under PyQt5 (the
        # process aborts). Anything that goes wrong in here becomes a
        # connectionFailed signal with the traceback instead.
        try:
            self._run()
        except Exception:
            import traceback
            tb = traceback.format_exc()
            sys.stderr.write(tb)
            self.connectionFailed.emit(tb.strip().splitlines()[-1])
            self._teardown()
            self.disconnected.emit()

    def _run(self):
        """
        The session, set up EXACTLY like examples/screenshot.py - which is
        known to work against real Windows hosts:

            context_new -> settings -> freerdp_connect -> gdi_init -> pump

        No FreeRDP callback is overridden and nothing is registered inside
        FreeRDP unless a feature that needs it is switched on. FreeRDP's own
        defaults decide codecs, channels and the Graphics Pipeline.
        """
        a = self._api
        entry = CLIENT.RDP_CLIENT_ENTRY_POINTS_V1()
        entry.Size = ctypes.sizeof(entry)
        entry.Version = CLIENT.RDP_CLIENT_INTERFACE_VERSION
        entry.ContextSize = ctypes.sizeof(F.rdpContext)
        self._ctx = a.freerdp_client_context_new(ctypes.byref(entry))
        if not self._ctx:
            self.connectionFailed.emit("freerdp_client_context_new failed")
            self.disconnected.emit()
            return
        ctx = self._ctx.contents
        self._instance = ctx.instance

        # --- settings: the example's set, plus desktop size ------------------
        s = ctx.settings
        a.freerdp_settings_set_string(s, KEY.FreeRDP_ServerHostname, self._host.encode())
        a.freerdp_settings_set_uint32(s, KEY.FreeRDP_ServerPort, self._port)
        if self._user:
            a.freerdp_settings_set_string(s, KEY.FreeRDP_Username, self._user.encode())
        if self._password:
            a.freerdp_settings_set_string(s, KEY.FreeRDP_Password, self._password.encode())
        if self._domain:
            a.freerdp_settings_set_string(s, KEY.FreeRDP_Domain, self._domain.encode())
        a.freerdp_settings_set_bool(s, KEY.FreeRDP_IgnoreCertificate, True)
        a.freerdp_settings_set_bool(s, KEY.FreeRDP_SoftwareGdi, True)
        a.freerdp_settings_set_uint32(s, KEY.FreeRDP_ColorDepth, 32)
        a.freerdp_settings_set_uint32(s, KEY.FreeRDP_DesktopWidth, self._size.width())
        a.freerdp_settings_set_uint32(s, KEY.FreeRDP_DesktopHeight, self._size.height())
        # Legacy codecs. Pure settings (no callbacks); xfreerdp advertises the
        # same, and FreeRDP's sample server drops clients that offer neither
        # RemoteFX nor NSCodec.
        a.freerdp_settings_set_bool(s, KEY.FreeRDP_RemoteFxCodec, True)
        a.freerdp_settings_set_bool(s, KEY.FreeRDP_NSCodec, True)
        a.freerdp_settings_set_bool(s, KEY.FreeRDP_SurfaceCommandsEnabled, True)
        a.freerdp_settings_set_bool(s, KEY.FreeRDP_FrameMarkerCommandEnabled, True)
        a.freerdp_settings_set_bool(s, KEY.FreeRDP_FastPathOutput, True)

        # channel events: needed for the clipboard (cliprdr) and, when enabled,
        # the Graphics Pipeline. One callback per channel, at connect time.
        self._subscribe_channel_events()
        if self.clipboard and self.clipboardBridge is not None:
            a.freerdp_settings_set_bool(s, KEY.FreeRDP_RedirectClipboard, True)
            self._add_static_channel(s, [b"cliprdr"])

        # --- opt-in extras (each adds a place FreeRDP calls back into Python) --
        if self.graphics_pipeline:
            # rdpgfx must be attached to the GDI when the channel connects,
            # which can only be arranged before freerdp_connect
            a.freerdp_settings_set_bool(s, KEY.FreeRDP_SupportGraphicsPipeline, True)
            for key in (KEY.FreeRDP_GfxH264, KEY.FreeRDP_GfxAVC444, KEY.FreeRDP_GfxAVC444v2):
                a.freerdp_settings_set_bool(s, key, bool(self.h264))
        if self.use_qt_sound:
            from . import rdpqtsoundplugin
            if rdpqtsoundplugin.register():
                a.freerdp_settings_set_bool(s, KEY.FreeRDP_AudioPlayback, True)
                self._add_static_channel(s, [b"rdpsnd", b"sys:" + rdpqtsoundplugin.SUBSYSTEM])

        # --- connect, then the GDI - exactly the example's order ------------
        self.aboutToConnect.emit()
        if not a.freerdp_connect(self._instance):
            code = a.freerdp_get_last_error(self._ctx)
            msg = a.freerdp_get_last_error_string(code)
            self.connectionFailed.emit("{0} (0x{1:08X})".format(
                msg.decode() if msg else "connect failed", code))
            self._teardown()
            self.disconnected.emit()
            return

        if not a.gdi_init(self._instance, COLOR.PIXEL_FORMAT_BGRX32):
            self.connectionFailed.emit("gdi_init failed")
            a.freerdp_disconnect(self._instance)
            self._teardown()
            self.disconnected.emit()
            return
        self._gdi = ctx.gdi
        # keyboard tables (X11 keycode -> RDP scancode etc.); without this
        # freerdp_keyboard_get_rdp_scancode_from_x11_keycode() returns 0 for
        # every key. xfreerdp does the same in its PreConnect.
        a.freerdp_keyboard_init(a.freerdp_settings_get_uint32(s, KEY.FreeRDP_KeyboardLayout))
        self._cur = self._prev = None
        self._last_frame_time = 0.0
        if self.remote_cursor:
            self._register_pointer()
        self.connected.emit()

        self.loop.exec(self._ctx)

        a.freerdp_disconnect(self._instance)
        if self._gdi:
            a.gdi_free(self._instance)
            self._gdi = None
        self._teardown()
        self.disconnected.emit()

    def _subscribe_channel_events(self):
        ctx = self._ctx.contents
        self._on_channel_connected = T.pChannelConnectedEventHandler(self._channel_connected)
        self._on_channel_disconnected = T.pChannelDisconnectedEventHandler(self._channel_disconnected)
        self._refs += [self._on_channel_connected, self._on_channel_disconnected]
        self._api.PubSub_Subscribe(ctx.pubSub, b"ChannelConnected", self._on_channel_connected)
        self._api.PubSub_Subscribe(ctx.pubSub, b"ChannelDisconnected", self._on_channel_disconnected)

    def _register_pointer(self):
        ctx = self._ctx.contents
        self._pointer_sink.setPalette(ctypes.pointer(self._gdi.contents.palette))
        pointer = T.rdpPointer()
        pointer.size = ctypes.sizeof(T.rdpPointer)
        pointer.New = T.pPointer_New(self._pointer_new)
        pointer.Free = T.pPointer_Free(self._pointer_free)
        pointer.Set = T.pPointer_Set(self._pointer_set)
        pointer.SetNull = T.pPointer_SetNull(self._pointer_set_null)
        pointer.SetDefault = T.pPointer_SetDefault(self._pointer_set_default)
        pointer.SetPosition = T.pPointer_SetPosition(self._pointer_set_position)
        self._refs += [pointer.New, pointer.Free, pointer.Set, pointer.SetNull,
                       pointer.SetDefault, pointer.SetPosition, pointer]
        self._api.graphics_register_pointer(ctx.graphics, ctypes.byref(pointer))

    def _add_static_channel(self, settings, args):
        """freerdp_client_add_static_channel(settings, argc, argv) with a char*[]."""
        argv = (ctypes.c_char_p * len(args))(*args)
        return bool(self._api.freerdp_client_add_static_channel(settings, len(args), argv))

    def _teardown(self):
        if self._ctx:
            self._api.freerdp_client_context_free(self._ctx)
        self._ctx = self._instance = self._gdi = None

    # --- queries -----------------------------------------------------------------
    def framebuffer(self):
        """(width, height, stride, buffer pointer) of the GDI framebuffer, or None."""
        if not self._gdi:
            return None
        g = self._gdi.contents
        return g.width, g.height, g.stride, g.primary_buffer
