# coding=utf-8
"""
ClipboardBridge - text clipboard between the local Qt clipboard and the
remote session, over FreeRDP's cliprdr channel.
Protocol (MS-RDPECLIP), client side:
  server -> MonitorReady            : we send ClientCapabilities and our
                                      ClientFormatList (what we can provide)
  server -> ServerFormatList        : remote clipboard changed; if it offers
                                      text we ask for it (ClientFormatDataRequest)
  server -> ServerFormatDataResponse: the text arrives -> local clipboard
  server -> ServerFormatDataRequest : remote wants our text ->
                                      ClientFormatDataResponse
  local clipboard changed           : we announce a new ClientFormatList
All cliprdr callbacks and Client* calls happen on the RDP thread. QClipboard
is GUI-thread only, so the two sides talk through queued signals; the local
text is cached on the client so a ServerFormatDataRequest can be answered
immediately on the RDP thread.
"""
import ctypes
from pyfreerdpnative import types as T
from pyfreerdpnative import constants as C
from pyfreerdpnative.freerdp.channels import cliprdr as CH
from ManyQt.QtCore import QObject, Qt, Signal, Slot
from ManyQt.QtWidgets import QApplication

CLIPRDR_CHANNEL = CH.CLIPRDR_SVC_CHANNEL_NAME.encode()


def _guard(default):
    def deco(fn):
        def wrapper(self, *a):
            try:
                return fn(self, *a)
            except Exception:
                import sys
                import traceback
                sys.stderr.write("[remotedisplay.clipboard] exception in {0}:\n{1}".format(
                    fn.__name__, traceback.format_exc()))
                return default

        wrapper.__name__ = fn.__name__
        return wrapper

    return deco


class ClipboardBridge(QObject):
    # RDP thread -> GUI thread
    remoteTextAvailable = Signal(str)
    # GUI thread -> RDP thread
    _localTextChanged = Signal(str)

    def __init__(self, api, parent=None):
        QObject.__init__(self, parent)
        self._api = api
        self._ctx = None  # POINTER(CliprdrClientContext)
        self._refs = []  # callbacks handed to C must stay alive
        self._local_text = ""  # cached copy of the local clipboard
        self._remote_has_text = False
        self._ignore_next_local = False
        self._localTextChanged.connect(self._onLocalTextChanged, Qt.QueuedConnection)

    # --- GUI thread -------------------------------------------------------------
    def attachLocalClipboard(self):
        """Call on the GUI thread once a QApplication exists."""
        cb = QApplication.clipboard()
        cb.dataChanged.connect(self._onQtClipboardChanged)
        self._onQtClipboardChanged()

    def _onQtClipboardChanged(self):
        if self._ignore_next_local:  # we just set it from remote data
            self._ignore_next_local = False
            return
        text = QApplication.clipboard().text()
        if text:
            self._localTextChanged.emit(text)  # queued to the RDP thread

    @Slot(str)
    def setLocalText(self, text):
        """GUI thread: remote text arrived; put it on the local clipboard."""
        self._ignore_next_local = True
        QApplication.clipboard().setText(text)

    # --- RDP thread: channel attach ---------------------------------------------
    def attach(self, iface):
        """Called from ChannelConnected for 'cliprdr' with e->pInterface."""
        self._ctx = ctypes.cast(iface, ctypes.POINTER(T.CliprdrClientContext))
        c = self._ctx.contents
        binds = {
            "MonitorReady": self._monitor_ready,
            "ServerCapabilities": self._server_capabilities,
            "ServerFormatList": self._server_format_list,
            "ServerFormatListResponse": self._server_format_list_response,
            "ServerFormatDataRequest": self._server_format_data_request,
            "ServerFormatDataResponse": self._server_format_data_response,
        }
        fields = dict((f, t) for f, t, *_ in T.CliprdrClientContext._fields_)
        for name, fn in binds.items():
            cb = fields[name](fn)
            self._refs.append(cb)
            setattr(c, name, cb)

    def detach(self):
        self._ctx = None

    # --- RDP thread: outgoing ----------------------------------------------------
    def _send_capabilities(self):
        general = T.CLIPRDR_GENERAL_CAPABILITY_SET()
        general.capabilitySetType = CH.CB_CAPSTYPE_GENERAL
        general.capabilitySetLength = 12
        general.version = CH.CB_CAPS_VERSION_2
        general.generalFlags = CH.CB_USE_LONG_FORMAT_NAMES
        caps = T.CLIPRDR_CAPABILITIES()
        caps.cCapabilitiesSets = 1
        caps.capabilitySets = ctypes.cast(ctypes.pointer(general), ctypes.POINTER(T.CLIPRDR_CAPABILITY_SET))
        return self._ctx.contents.ClientCapabilities(self._ctx, ctypes.byref(caps))

    def _send_format_list(self):
        """Announce what we can provide: text, if we have any."""
        formats = (T.CLIPRDR_FORMAT * 2)()
        n = 0
        if self._local_text:
            formats[0].formatId = C.CF_UNICODETEXT
            formats[1].formatId = C.CF_TEXT
            n = 2
        fl = T.CLIPRDR_FORMAT_LIST()
        fl.numFormats = n
        fl.formats = ctypes.cast(formats, ctypes.POINTER(T.CLIPRDR_FORMAT)) if n else None
        return self._ctx.contents.ClientFormatList(self._ctx, ctypes.byref(fl))

    def _send_format_list_response(self, ok=True):
        r = T.CLIPRDR_FORMAT_LIST_RESPONSE()
        r.common.msgFlags = CH.CB_RESPONSE_OK if ok else CH.CB_RESPONSE_FAIL
        return self._ctx.contents.ClientFormatListResponse(self._ctx, ctypes.byref(r))

    def _request_text(self):
        req = T.CLIPRDR_FORMAT_DATA_REQUEST()
        req.requestedFormatId = C.CF_UNICODETEXT
        return self._ctx.contents.ClientFormatDataRequest(self._ctx, ctypes.byref(req))

    def _send_data_response(self, payload):
        resp = T.CLIPRDR_FORMAT_DATA_RESPONSE()
        if payload is None:
            resp.common.msgFlags = CH.CB_RESPONSE_FAIL
            resp.common.dataLen = 0
            resp.requestedFormatData = None
        else:
            buf = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
            resp.common.msgFlags = CH.CB_RESPONSE_OK
            resp.common.dataLen = len(payload)
            resp.requestedFormatData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
            self._last_payload = buf  # keep alive until sent
        return self._ctx.contents.ClientFormatDataResponse(self._ctx, ctypes.byref(resp))

    @Slot(str)
    def _onLocalTextChanged(self, text):
        self._local_text = text
        if self._ctx:
            self._send_format_list()

    # --- RDP thread: callbacks from cliprdr ---------------------------------------
    @_guard(0)
    def _monitor_ready(self, ctx, ready):
        rc = self._send_capabilities()
        if rc == 0:
            rc = self._send_format_list()
        return rc

    @_guard(0)
    def _server_capabilities(self, ctx, caps):
        return 0

    @_guard(0)
    def _server_format_list(self, ctx, fl):
        f = fl.contents
        ids = set(int(f.formats[i].formatId) for i in range(int(f.numFormats))) if f.formats else set()
        self._remote_has_text = bool(ids & {C.CF_UNICODETEXT, C.CF_TEXT})
        self._send_format_list_response(True)
        if self._remote_has_text:
            self._request_text()  # pull the text right away
        return 0

    @_guard(0)
    def _server_format_list_response(self, ctx, resp):
        return 0

    @_guard(0)
    def _server_format_data_request(self, ctx, req):
        fmt = int(req.contents.requestedFormatId)
        text = self._local_text
        if not text:
            return self._send_data_response(None)
        if fmt == C.CF_UNICODETEXT:
            payload = text.replace("\n", "\r\n").encode("utf-16-le") + b"\x00\x00"
        elif fmt == C.CF_TEXT:
            payload = text.replace("\n", "\r\n").encode("cp1252", "replace") + b"\x00"
        else:
            return self._send_data_response(None)
        return self._send_data_response(payload)

    @_guard(0)
    def _server_format_data_response(self, ctx, resp):
        r = resp.contents
        n = int(r.common.dataLen)
        if r.common.msgFlags & CH.CB_RESPONSE_FAIL or n == 0 or not r.requestedFormatData:
            return 0
        raw = ctypes.string_at(r.requestedFormatData, n)
        text = raw.decode("utf-16-le", "replace").split("\x00", 1)[0].replace("\r\n", "\n")
        self.remoteTextAvailable.emit(text)  # queued to the GUI thread
        return 0
