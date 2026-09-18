# coding=utf-8
"""
RdpQtSoundPlugin - lets FreeRDP play audio through Qt Multimedia.
Port of rdpqtsoundplugin.cpp. It implements rdpsnd's device-plugin interface
(freerdp/client/rdpsnd.h: rdpsndDevicePlugin) around QAudioOutput (Qt 5) /
QAudioSink (Qt 6), and is plugged into FreeRDP the same way the C++ was:
  * freerdp_register_addin_provider() installs a hook that FreeRDP asks for
    every channel addin; for ("rdpsnd", subsystem "qt") it returns our entry
    point, for anything else it defers to FreeRDP's own static table.
  * the client asks for that subsystem with the static channel
    ("rdpsnd", "sys:qt").
FreeRDP hands Play() PCM that rdpsnd already decoded (the DSP handles ADPCM,
AAC, ... upstream), so this only has to accept PCM formats and feed bytes to
the Qt audio sink.
"""
import ctypes
from ManyQt.QtCore import Signal, Slot, QObject, QCoreApplication
from pyfreerdpnative import load
from pyfreerdpnative import types as T
from pyfreerdpnative.freerdp.channels import rdpsnd as CH_RDPSND  # noqa: F401  (constants)
from pyfreerdpnative.freerdp.codec import audio as AUDIO

try:
    from ManyQt.QtMultimedia import QAudioFormat, QAudioSink

    HAVE_QT_MULTIMEDIA = True
except ImportError:  # QtMultimedia is an optional Qt module
    HAVE_QT_MULTIMEDIA = False

SUBSYSTEM = b"qt"
_api = None
_refs = []  # callbacks handed to C must outlive the session
_devices = {}  # id(rdpsndDevicePlugin address) -> _QtSoundDevice


def api():
    global _api
    if _api is None:
        _api = load()
    return _api


# --------------------------------------------------------------------------
# The Qt side, always on the GUI thread (QAudioOutput is not thread-safe)
# --------------------------------------------------------------------------
class _QtAudio(QObject):
    _open = Signal(int, int, int)  # rate, channels, bits
    _write = Signal(bytes)
    _close = Signal()
    _volume = Signal(float)

    def __init__(self, *args, **kwargs):
        super(_QtAudio, self).__init__(*args, **kwargs)
        self._sink = None
        self._io = None
        self._open.connect(self._on_open)
        self._write.connect(self._on_write)
        self._close.connect(self._on_close)
        self._volume.connect(self._on_volume)
        app = QCoreApplication.instance()
        if app is not None:
            self.moveToThread(app.thread())

    @Slot(int, int, int)
    def _on_open(self, rate, channels, bits):
        self._on_close()
        fmt = QAudioFormat()
        fmt.setSampleRate(rate)
        fmt.setChannelCount(channels)
        if hasattr(fmt, 'setSampleSize') and hasattr(QAudioFormat, 'LittleEndian'):
            fmt.setSampleSize(bits)
            fmt.setCodec("audio/pcm")
            fmt.setByteOrder(QAudioFormat.LittleEndian)
            fmt.setSampleType(QAudioFormat.SignedInt if bits > 8 else QAudioFormat.UnSignedInt)
        else:
            fmt.setSampleFormat(QAudioFormat.Int16 if bits == 16 else QAudioFormat.UInt8)
        self._sink = QAudioSink(fmt)
        self._io = self._sink.start()

    @Slot(bytes)
    def _on_write(self, data):
        if self._io is not None:
            self._io.write(data)

    @Slot()
    def _on_close(self):
        if self._sink is not None:
            self._sink.stop()
        self._sink = self._io = None

    @Slot(float)
    def _on_volume(self, v):
        if self._sink is not None:
            self._sink.setVolume(v)


# --------------------------------------------------------------------------
# rdpsndDevicePlugin implementation (called on FreeRDP's thread)
# --------------------------------------------------------------------------
class _QtSoundDevice(object):
    def __init__(self):
        self.qt = _QtAudio() if HAVE_QT_MULTIMEDIA else None
        self.format = None
        self.volume = 0xFFFFFFFF  # left/right 16-bit each, full
        self.latency_ms = 0
        self.plays = 0
        self.bytes = 0

    # BOOL FormatSupported(device, format)
    def format_supported(self, dev, fmt):
        f = fmt.contents
        return bool(f.wFormatTag == AUDIO.WAVE_FORMAT_PCM and f.nChannels in (1, 2)
                    and f.wBitsPerSample in (8, 16) and f.nSamplesPerSec > 0)

    # BOOL Open(device, format, latency)
    def open(self, dev, fmt, latency):
        f = fmt.contents
        self.format = (f.nSamplesPerSec, f.nChannels, f.wBitsPerSample)
        self.latency_ms = int(latency)
        if self.qt is not None:
            self.qt._open.emit(*self.format)
        return True

    # UINT Play(device, data, size)   -> latency in ms
    def play(self, dev, data, size):
        n = int(size)
        if n > 0:
            self.plays += 1
            self.bytes += n
            if self.qt is not None:
                self.qt._write.emit(ctypes.string_at(data, n))
        return self.latency_ms

    def start(self, dev):
        pass

    def close(self, dev):
        if self.qt is not None:
            self.qt._close.emit()

    def free(self, dev):
        self.close(dev)
        _devices.pop(ctypes.addressof(dev.contents), None)

    def set_volume(self, dev, value):
        self.volume = int(value)
        left, right = value & 0xFFFF, (value >> 16) & 0xFFFF
        if self.qt is not None:
            self.qt._volume.emit(max(left, right) / 65535.0)
        return True

    def get_volume(self, dev):
        return self.volume


def _make_plugin_struct(device):
    """Fill an rdpsndDevicePlugin with CFUNCTYPE thunks into `device`."""
    fields = dict((f, t) for f, t, *_ in T.rdpsndDevicePlugin._fields_)
    plugin = T.rdpsndDevicePlugin()
    thunks = {
        "FormatSupported": device.format_supported, "Open": device.open,
        "Play": device.play, "Start": device.start, "Close": device.close,
        "Free": device.free, "SetVolume": device.set_volume, "GetVolume": device.get_volume,
    }
    for name, fn in thunks.items():
        cb = fields[name](fn)
        _refs.append(cb)
        setattr(plugin, name, cb)
    _refs.append(plugin)
    return plugin


def _entry(entry_points):
    """PFREERDP_RDPSND_DEVICE_ENTRY: FreeRDP calls this to create the device."""
    ep = entry_points.contents
    device = _QtSoundDevice()
    plugin = _make_plugin_struct(device)
    plugin.rdpsnd = ep.rdpsnd
    _devices[ctypes.addressof(plugin)] = device
    ep.pRegisterRdpsndDevice(ep.rdpsnd, ctypes.byref(plugin))
    return 0  # CHANNEL_RC_OK


def _addin_provider(name, subsystem, type_, flags):
    """FREERDP_LOAD_CHANNEL_ADDIN_ENTRY_FN: intercept rdpsnd/qt, defer the rest."""
    n = ctypes.cast(name, ctypes.c_char_p).value if name else None
    s = ctypes.cast(subsystem, ctypes.c_char_p).value if subsystem else None
    t = ctypes.cast(type_, ctypes.c_char_p).value if type_ else None
    if n == b"rdpsnd" and s == SUBSYSTEM:
        return ctypes.cast(_entry_cb, ctypes.c_void_p).value
    fallback = api().freerdp_channels_load_static_addin_entry(n, s, t, flags)
    return ctypes.cast(fallback, ctypes.c_void_p).value if fallback else None


# ctypes cannot make a callback whose RESULT is a function-pointer type, so
# the provider is declared returning void* (the C ABI is identical) and cast
# to FreeRDP's FREERDP_LOAD_CHANNEL_ADDIN_ENTRY_FN when registered.
_ProviderType = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
                                 ctypes.c_char_p, ctypes.c_uint32)
_entry_cb = T.PFREERDP_RDPSND_DEVICE_ENTRY(_entry)
_provider_cb = _ProviderType(_addin_provider)
_refs += [_entry_cb, _provider_cb]


def register():
    """
    Install the addin provider. Call it AFTER freerdp_client_context_new():
    that function registers FreeRDP's default provider itself (client/common/
    client.c), replacing anything installed earlier - so a once-per-process
    registration works for the first session and silently breaks the next.
    Returns False when QtMultimedia is unavailable.
    """
    if not HAVE_QT_MULTIMEDIA:
        return False
    api().freerdp_register_addin_provider(
        ctypes.cast(_provider_cb, T.FREERDP_LOAD_CHANNEL_ADDIN_ENTRY_FN), 0)
    return True


def active_devices():
    """
    The live device objects (for diagnostics).
    """
    return list(_devices.values())
