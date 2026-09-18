# coding=utf-8
"""
Screen buffers: the remote framebuffer and two view transforms over it.
    RemoteScreenBuffer      the desktop as FreeRDP's software GDI paints it
    ScaledScreenBuffer      aspect-preserving scale to the widget
    LetterboxedScreenBuffer centre the scaled image, black bars around
Each has createImage() and (for the transforms) mapToSource() to translate
widget coordinates back to remote-desktop pixels, exactly like the C++.
"""
from ManyQt.QtGui import QPixmap, QPainter, QImage, QTransform
from ManyQt.QtCore import QSize, QRect, Qt, QPoint


class ScreenBuffer(object):
    """
    ScreenBuffer class.
    """

    def createImage(self):
        """
        :return: QImage
        """
        raise NotImplementedError


class RemoteScreenBuffer(ScreenBuffer):
    """
    The remote desktop as an image the widget owns (the RDP thread hands over
    rectangles of pixels, exactly the C++ design), plus a cached copy scaled
    to the view. A dirty band is scaled on its own (0.3 ms for 16 rows at
    1080p) into the cache, so painting is a plain blit - never a rescale of
    the whole desktop.
    """

    def __init__(self, width, height):
        self.width, self.height = 0, 0
        self._image = QImage()
        self._scaled = QPixmap()
        self._view = QSize()
        self.resize(width, height)

    def resize(self, width, height):
        if (width, height) == (self.width, self.height) or width <= 0 or height <= 0:
            return False
        self.width, self.height = width, height
        img = QImage(width, height, QImage.Format_RGB32)
        img.fill(0)
        if not self._image.isNull():  # keep what we had
            p = QPainter(img)
            p.drawImage(0, 0, self._image)
            p.end()
        self._image = img
        self._rebuildScaled()
        return True

    def setViewSize(self, size):
        """Size of the area the desktop is drawn into (the letterbox rect)."""
        if size == self._view or size.isEmpty():
            return
        self._view = QSize(size)
        self._rebuildScaled()

    def _rebuildScaled(self):
        if self._view.isEmpty() or self._image.isNull():
            self._scaled = QPixmap()
            return
        if self._view == self._image.size():
            self._scaled = QPixmap.fromImage(self._image)  # 1:1, no scaling
        else:
            self._scaled = QPixmap.fromImage(self._image.scaled(
                self._view, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))

    def addRectangle(self, rect, data, bytes_per_row):
        """BGRX32 pixels for `rect`, as copied by the RDP thread."""
        if rect.isEmpty() or self._image.isNull():
            return
        src = QImage(data, rect.width(), rect.height(), bytes_per_row, QImage.Format_RGB32)
        if self._scaled.isNull() or self._view == self._image.size():
            # 1:1 (or no view yet): compose at full resolution, blit as is
            p = QPainter(self._image)
            p.drawImage(rect.topLeft(), src)
            p.end()
            if not self._scaled.isNull():
                p = QPainter(self._scaled)
                p.drawImage(rect.topLeft(), src)
                p.end()
            return
        # Scaled view: scale the band STRAIGHT from the incoming pixels into
        # the cache. The full-resolution image is not updated (it is only
        # needed to rebuild the cache on a view resize, and the client is
        # asked for a full frame then), which saves a full-res blit plus a
        # copy per band.
        sx = self._view.width() / float(self.width)
        sy = self._view.height() / float(self.height)
        t2 = QRect(int(rect.x() * sx), int(rect.y() * sy),
                   max(1, int(rect.width() * sx + 1)), max(1, int(rect.height() * sy + 1)))
        # smooth scaling for small bands (text, cursor), fast for large ones
        # (scrolling, video) where its cost would dominate the frame
        mode = Qt.SmoothTransformation if rect.height() <= 64 else Qt.FastTransformation
        piece = src.scaled(t2.size(), Qt.IgnoreAspectRatio, mode)
        p = QPainter(self._scaled)
        p.drawImage(t2.topLeft(), piece)
        p.end()

    def scaledPixmap(self):
        return self._scaled

    def createImage(self):
        return self._image


class ScaledScreenBuffer(ScreenBuffer):
    def __init__(self, source):
        self.source = source
        self.scaledSize = source.createImage().size()
        self._transform = QTransform()

    def createImage(self):
        src = self.source.createImage()
        if src.isNull():
            return QImage()
        return src.scaled(self.scaledSize, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)

    def scaleToFit(self, size):
        src = QSize(self.source.width, self.source.height)
        if src.isEmpty():
            return
        scaled = QSize(src)
        scaled.scale(size, Qt.KeepAspectRatio)
        if scaled.isEmpty():
            return
        self.scaledSize = scaled
        self._transform = QTransform()
        self._transform.scale(src.width() / float(scaled.width()), src.height() / float(scaled.height()))

    def mapToSource(self, point):
        return self._transform.map(point)


class LetterboxedScreenBuffer(ScreenBuffer):
    """
    LetterboxedScreenBuffer class.
    """

    def __init__(self, source):
        self.source = source
        self.size = QSize()
        self.sourceRect = QRect()
        self._transform = QTransform()

    def createImage(self):
        src = self.source.createImage()
        if src.isNull() or self.size.isEmpty():
            return QImage()
        img = QImage(self.size, src.format())
        painter = QPainter(img)
        painter.fillRect(img.rect(), Qt.black)
        painter.drawImage(self.sourceRect, src)
        painter.end()
        return img

    def mapToSource(self, point):
        p = self._transform.map(point)
        return QPoint(min(max(p.x(), 0), max(self.sourceRect.width() - 1, 0)),
                      min(max(p.y(), 0), max(self.sourceRect.height() - 1, 0)))

    def resize(self, size):
        self.size = QSize(size)
        self.sourceRect = QRect(QPoint(0, 0), self.source.scaledSize)
        self.sourceRect.moveCenter(QPoint(size.width() // 2, size.height() // 2))
        self._transform = QTransform()
        self._transform.translate(-self.sourceRect.left(), -self.sourceRect.top())
