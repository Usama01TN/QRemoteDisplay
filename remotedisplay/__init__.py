# coding=utf-8
"""
remotedisplay - an RDP display widget for ManyQt, built on
pyfreerdpnative (FreeRDP 3). Python port of RemoteDisplay by Jarno Puff.
"""
from os.path import dirname
from sys import path

if dirname(__file__) not in path:
    path.append(dirname(__file__))

try:
    from .screenbuffer import LetterboxedScreenBuffer, RemoteScreenBuffer, ScaledScreenBuffer  # noqa: F401
    from .cursorchangenotifier import CursorChangeNotifier  # noqa: F401
    from .remotedisplaywidget import RemoteDisplayWidget  # noqa: F401
    from .rdpqtsoundplugin import HAVE_QT_MULTIMEDIA  # noqa: F401
    from .freerdpeventloop import FreeRdpEventLoop  # noqa: F401
    from .freerdpclient import FreeRdpClient  # noqa: F401
    from .clipboard import ClipboardBridge  # noqa: F401
except:
    from screenbuffer import LetterboxedScreenBuffer, RemoteScreenBuffer, ScaledScreenBuffer  # noqa: F401
    from cursorchangenotifier import CursorChangeNotifier  # noqa: F401
    from remotedisplaywidget import RemoteDisplayWidget  # noqa: F401
    from rdpqtsoundplugin import HAVE_QT_MULTIMEDIA  # noqa: F401
    from freerdpeventloop import FreeRdpEventLoop  # noqa: F401
    from freerdpclient import FreeRdpClient  # noqa: F401
    from clipboard import ClipboardBridge  # noqa: F401

__all__ = ['RemoteDisplayWidget', 'FreeRdpClient', 'FreeRdpEventLoop', 'CursorChangeNotifier', 'RemoteScreenBuffer',
           'ScaledScreenBuffer', 'LetterboxedScreenBuffer']
