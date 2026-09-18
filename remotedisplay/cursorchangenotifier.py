# coding=utf-8
"""
Turns FreeRDP pointer updates into QCursors on the GUI thread.
FreeRDP calls Pointer_New / Pointer_Set / Pointer_Free on its own thread with
an rdpPointer (XOR colour data + AND mask). The C++ original decoded the two
masks by hand; FreeRDP 3 provides freerdp_image_copy_from_pointer_data(),
which yields straight BGRA32 with alpha - one QImage, no mask juggling.
QCursor objects must be created on the GUI thread, so only an index crosses
threads (a queued signal), as in the original.
"""
import ctypes
import threading
from ManyQt.QtCore import Signal, Slot, QObject, Qt
from ManyQt.QtGui import QCursor, QPixmap, QImage
from pyfreerdpnative.freerdp.codec import color as COLOR


class CursorChangeNotifier(QObject):
    cursorChanged = Signal(QCursor)
    _pointerChanged = Signal(int)  # index >= 0: a pointer; -1: hide; -2: default

    def __init__(self, api, parent=None):
        QObject.__init__(self, parent)
        self._api = api
        self._images = {}  # index -> (QImage, hotX, hotY)
        self._by_pointer = {}  # id(rdpPointer address) -> index
        self._next = 0
        self._lock = threading.Lock()
        self._palette = None  # POINTER(gdiPalette) of the session's GDI
        # queued: _pointerChanged is emitted from the RDP thread and the slot
        # builds a QCursor, which must happen on the GUI thread
        self._pointerChanged.connect(self._onPointerChanged, Qt.QueuedConnection)

    def setPalette(self, palette_ptr):
        self._palette = palette_ptr

    # --- called from the FreeRDP thread -------------------------------------
    MAX_DIMENSION = 384  # RDP allows 96x96; be generous, reject nonsense

    def addPointer(self, pointer):
        if not pointer:
            return False
        p = pointer.contents
        w, h = int(p.width), int(p.height)
        # Guard every input freerdp_image_copy_from_pointer_data dereferences:
        # a null or truncated mask makes it read out of bounds, which faults
        # inside FreeRDP (surfacing as a crash in check_event_handles).
        if not (0 < w <= self.MAX_DIMENSION and 0 < h <= self.MAX_DIMENSION):
            return False
        if not p.xorMaskData or p.lengthXorMask == 0:
            return False
        if p.xorBpp not in (1, 8, 16, 24, 32):
            return False
        if p.xorBpp == 8 and not self._palette:
            return False  # 8-bpp needs the palette
        if p.xorBpp == 1 and (not p.andMaskData or p.lengthAndMask == 0):
            return False  # 1bpp needs the AND mask
        stride = w * 4
        buf = (ctypes.c_ubyte * (stride * h))()
        and_data = p.andMaskData if p.andMaskData else None
        and_len = p.lengthAndMask if p.andMaskData else 0
        ok = self._api.freerdp_image_copy_from_pointer_data(
            buf, COLOR.PIXEL_FORMAT_BGRA32, stride, 0, 0, w, h,
            p.xorMaskData, p.lengthXorMask, and_data, and_len,
            p.xorBpp, self._palette)
        if not ok:
            return False
        img = QImage(bytes(buf), w, h, stride, QImage.Format_ARGB32).copy()
        hx = min(max(int(p.xPos), 0), w - 1)
        hy = min(max(int(p.yPos), 0), h - 1)
        with self._lock:
            idx = self._next
            self._next += 1
            self._images[idx] = (img, hx, hy)
            self._by_pointer[ctypes.addressof(p)] = idx
        return True

    def removePointer(self, pointer):
        if not pointer:
            return
        with self._lock:
            idx = self._by_pointer.pop(ctypes.addressof(pointer.contents), None)
            if idx is not None:
                self._images.pop(idx, None)

    def changePointer(self, pointer):
        if not pointer:
            return False
        with self._lock:
            idx = self._by_pointer.get(ctypes.addressof(pointer.contents))
        if idx is not None:
            self._pointerChanged.emit(idx)  # queued to the GUI thread
        return idx is not None

    def setNull(self):
        self._pointerChanged.emit(-1)

    def setDefault(self):
        self._pointerChanged.emit(-2)

    # --- GUI thread -----------------------------------------------------------
    @Slot(int)
    def _onPointerChanged(self, idx):
        if idx == -1:
            self.cursorChanged.emit(QCursor(Qt.BlankCursor))
            return
        if idx == -2:
            self.cursorChanged.emit(QCursor(Qt.ArrowCursor))
            return
        with self._lock:
            entry = self._images.get(idx)
        if entry is None:
            return
        img, hx, hy = entry
        self.cursorChanged.emit(QCursor(QPixmap.fromImage(img), hx, hy))
