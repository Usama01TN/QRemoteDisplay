# coding=utf-8
"""
RemoteDisplayWidget - a QWidget that shows a remote desktop.
    w = RemoteDisplayWidget()
    w.setDesktopSize(1280, 800)
    w.setCredentials("alice", "secret")
    w.connectToHost("10.0.0.5", 3389)
    w.disconnected.connect(app.quit)
Mouse and keyboard go to the remote host; the remote cursor shape is applied
to the widget; repaints are rate-limited to FRAMERATE_LIMIT as in the C++.
"""
from ManyQt.QtCore import Signal, Slot, QRect, QThread, QSize, QPoint, Qt
from ManyQt.QtGui import QCursor, QPainter
from ManyQt.QtWidgets import QWidget
from .clipboard import ClipboardBridge
from .cursorchangenotifier import CursorChangeNotifier
from .freerdpclient import FreeRdpClient, api
from .screenbuffer import LetterboxedScreenBuffer, RemoteScreenBuffer, ScaledScreenBuffer

FRAMERATE_LIMIT = 40


class RemoteDisplayWidget(QWidget):
    aboutToConnect = Signal()
    connected = Signal()
    disconnected = Signal()
    connectionFailed = Signal(str)

    def __init__(self, *args, **kwargs):
        super(RemoteDisplayWidget, self).__init__(*args, **kwargs)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        self._desktopSize = QSize()
        self._credentials = ("", "", "")
        self._remote = self._scaled = self._letterboxed = None
        self._repaintNeeded = False
        self._dirty = QRect()

        # FreeRDP runs on its own thread; the client QObject lives there
        self._thread = QThread(self)
        self._cursorNotifier = CursorChangeNotifier(api(), self)
        self._cursorNotifier.cursorChanged.connect(self._onCursorChanged)
        self._client = FreeRdpClient(self._cursorNotifier)
        # clipboard: bridge lives on the RDP thread with the client; its GUI
        # side (QClipboard) is driven through queued signals
        self._clipboard = ClipboardBridge(api(), self._client)
        self._clipboard.remoteTextAvailable.connect(self._clipboard.setLocalText, Qt.QueuedConnection)
        self._clipboard.attachLocalClipboard()
        self._client.clipboardBridge = self._clipboard
        self._client.moveToThread(self._thread)
        # run() must execute ON the worker thread. Starting it from the
        # thread's own started signal guarantees that; a queued signal
        # connected before moveToThread() would instead run it on the GUI
        # thread (the object's affinity at connect time), blocking the GUI
        # and making every "queued" input slot run on the GUI thread too.
        self._thread.started.connect(self._client._threadStarted)
        self._client.aboutToConnect.connect(self.aboutToConnect)
        self._client.connected.connect(self._onConnected)
        self._client.disconnected.connect(self._onDisconnected)
        self._client.connectionFailed.connect(self.connectionFailed)
        self._client.desktopUpdated.connect(self._onDesktopUpdated)
        self._client.desktopRectangle.connect(self._onDesktopRectangle)
        self._client.desktopFrame.connect(self._onDesktopFrame)
        self._thread.start()

        # No repaint timer: the RDP thread already limits frames to
        # FRAMERATE_LIMIT, and a second sampling stage here only aliases
        # against the first (visible as an irregular frame rate). Each
        # rectangle triggers update(rect); Qt coalesces them per event-loop
        # pass into one paintEvent.

    # --- public API (same names as the C++) --------------------------------------
    def setDesktopSize(self, width, height):
        self._desktopSize = QSize(width, height)

    def setCredentials(self, user, password, domain=""):
        self._credentials = (user, password, domain)

    def connectToHost(self, host, port=3389):
        size = self._desktopSize if self._desktopSize.isValid() else self.size()
        self._client.configure(host, port, size.width(), size.height(), *self._credentials)
        self._client.start()

    def disconnectFromHost(self):
        self._client.requestStop()

    def sizeHint(self):
        return self._desktopSize if self._desktopSize.isValid() else QWidget.sizeHint(self)

    # --- client signals ------------------------------------------------------------
    @Slot()
    def _onConnected(self):
        fb = self._client.framebuffer()
        w, h = (fb[0], fb[1]) if fb else (self._desktopSize.width(), self._desktopSize.height())
        self._remote = RemoteScreenBuffer(w, h)
        self._scaled = ScaledScreenBuffer(self._remote)
        self._letterboxed = LetterboxedScreenBuffer(self._scaled)
        self._resizeScreenBuffers()
        self.connected.emit()

    @Slot(list, int, int, int)
    def _onDesktopFrame(self, bands, stride, desktop_w, desktop_h):
        if self._remote is None:
            return
        if self._remote.resize(desktop_w, desktop_h):       # GFX reset / display resize
            self._resizeScreenBuffers()
        dirty = QRect()
        for rect, data in bands:
            self._remote.addRectangle(rect, data, stride)
            dirty = dirty.united(self._mapFromRemoteDesktop(rect))
        if not dirty.isNull():
            self.update(dirty.adjusted(-2, -2, 2, 2))      # one repaint per frame

    @Slot(QRect, bytes, int, int, int)
    def _onDesktopRectangle(self, rect, data, bytes_per_row, desktop_w, desktop_h):
        if self._remote is None:
            return
        if self._remote.resize(desktop_w, desktop_h):       # GFX reset / display resize
            self._resizeScreenBuffers()
            self._dirty = self.rect()
        self._remote.addRectangle(rect, data, bytes_per_row)
        # only the widget area showing this rect needs repainting
        self.update(self._mapFromRemoteDesktop(rect).adjusted(-2, -2, 2, 2))

    @Slot()
    def _onDisconnected(self):
        self._remote = self._scaled = self._letterboxed = None
        self.unsetCursor()
        self.disconnected.emit()

    @Slot(QCursor)
    def _onCursorChanged(self, cursor):
        self.setCursor(cursor)

    @Slot(QRect)
    def _onDesktopUpdated(self, rect):
        pass                                  # repaint is driven by _onDesktopRectangle

    # --- geometry --------------------------------------------------------------------
    def _resizeScreenBuffers(self):
        if self._scaled:
            self._scaled.scaleToFit(self.size())
        if self._letterboxed:
            self._letterboxed.resize(self.size())
        if self._remote and self._letterboxed:
            self._remote.setViewSize(self._letterboxed.sourceRect.size())
            # the scaled cache was rebuilt from a possibly stale full-res image
            # (not updated while scaled): ask for a complete frame
            self._client.requestFullFrame()

    def _mapToRemoteDesktop(self, local):
        if self._scaled and self._letterboxed:
            return self._scaled.mapToSource(self._letterboxed.mapToSource(local))
        return QPoint()

    def _mapFromRemoteDesktop(self, remote_rect):
        """Remote-desktop rect -> widget rect (scale then letterbox offset)."""
        if not (self._scaled and self._letterboxed and self._remote.width and self._remote.height):
            return self.rect()
        target = self._letterboxed.sourceRect
        sx = target.width() / float(self._remote.width)
        sy = target.height() / float(self._remote.height)
        return QRect(int(target.x() + remote_rect.x() * sx), int(target.y() + remote_rect.y() * sy),
                     int(remote_rect.width() * sx) + 1, int(remote_rect.height() * sy) + 1)

    # --- Qt events -----------------------------------------------------------------
    def paintEvent(self, event):
        """
        Scale on paint, clipped to the dirty region: Qt only transforms the
        pixels inside the clip, so a small remote change costs a small paint
        instead of a full-frame rescale (which is what createImage() did).
        """
        painter = QPainter(self)
        painter.setClipRect(event.rect())
        if self._remote and self._letterboxed:
            pm = self._remote.scaledPixmap()
            if not pm.isNull():
                target = self._letterboxed.sourceRect
                # letterbox bars
                for bar in (QRect(0, 0, self.width(), target.top()),
                            QRect(0, target.bottom() + 1, self.width(), self.height() - target.bottom() - 1),
                            QRect(0, 0, target.left(), self.height()),
                            QRect(target.right() + 1, 0, self.width() - target.right() - 1, self.height())):
                    if bar.isValid() and bar.intersects(event.rect()):
                        painter.fillRect(bar, Qt.black)
                painter.drawPixmap(target.topLeft(), pm)      # blit; already scaled
                return
        painter.fillRect(self.rect(), Qt.black)

    def resizeEvent(self, event):
        self._resizeScreenBuffers()
        QWidget.resizeEvent(self, event)

    def mouseMoveEvent(self, event):
        self._client.sendMouseMoveEvent(self._mapToRemoteDesktop(event.pos()))

    def mousePressEvent(self, event):
        self._client.sendMousePressEvent(event.button(), self._mapToRemoteDesktop(event.pos()))

    def mouseReleaseEvent(self, event):
        self._client.sendMouseReleaseEvent(event.button(), self._mapToRemoteDesktop(event.pos()))

    def wheelEvent(self, event):
        self._client.sendWheelEvent(event.angleDelta().y())

    def keyPressEvent(self, event):
        self._client.sendKeyEvent(event)
        event.accept()

    def keyReleaseEvent(self, event):
        self._client.sendKeyEvent(event)
        event.accept()

    def closeEvent(self, event):
        self._shutdown()
        QWidget.closeEvent(self, event)

    def _shutdown(self):
        if self._thread.isRunning():
            self._client.requestStop()
            self._thread.quit()
            self._thread.wait(3000)

    def __del__(self):
        try:
            self._shutdown()
        except Exception:
            pass
