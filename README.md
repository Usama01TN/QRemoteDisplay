# RemoteDisplay

RemoteDisplay is an RDP client widget for [ManyQt](https://github.com/Usama01TN/ManyQt), built on
[pyfreerdpnative](https://github.com/Usama01TN/PyFreeRdpNative) (FreeRDP 3).
It is a class-for-class port of RemoteDisplay (Jolla Ltd, 2014).

The library consists of a single widget, `RemoteDisplayWidget`, which renders
the remote display inside it. The widget sends any mouse movement or keyboard
press to the remote host, shows the remote cursor shape, and plays back audio
through Qt Multimedia (or FreeRDP's own backend).

## Usage:

```python
from ManyQt.QtWidgets import QApplication          # or PySide6.QtWidgets
from sys import exit, argv

try:
    from .remotedisplay import RemoteDisplayWidget
except:
    from remotedisplay import RemoteDisplayWidget

app = QApplication(argv)
w = RemoteDisplayWidget()
w.resize(800, 600)
w.setDesktopSize(800, 600)
w.connectToHost('1.2.3.4', 3389)
w.setCredentials('alice', 'secret')               # not in the original; needed by real servers
w.show()
w.disconnected.connect(app.quit)                  # exit when disconnected
exit(app.exec_())
```

For the full example see [remotedisplay/main.py](remotedisplay/main.py):

```bash
python remotedisplay/main.py <host> <port> <width> <height> [user] [password]
```

## Installing:

```bash
pip install ManyQt
pip install PyQt5            # or: pip install PySide6
pip install pyfreerdpnative --find-links https://github.com/Usama01TN/PyFreeRdpNative/releases/expanded_assets/freerdp-libs-3.31.1
pip install .                # this package
```

Whichever Qt binding is installed is used (PyQt5 first if both;
`REMOTEDISPLAY_QT=pyside6` to prefer PySide6).

## Signals:

|                                              |                                                                                          |
|----------------------------------------------|------------------------------------------------------------------------------------------|
| `aboutToConnect()`                           | about to start the RDP handshake                                                         |
| `channelConnected(str)` (on `FreeRdpClient`) | a static or dynamic channel came up, e.g. `Microsoft::Windows::RDS::Graphics`            |
| `connected()`                                | session active, first frame may arrive any moment                                        |
| `disconnected()`                             | session ended (by the server, by `disconnectFromHost()`, or a failure)                   |
| `connectionFailed(str)`                      | why `freerdp_connect` failed, e.g. `The connection transport layer failed. (0x0002000D)` |

## How the original maps onto FreeRDP 3:

The C++ used FreeRDP 1.x APIs; each has an exact FreeRDP 3 counterpart, all
reached through pyfreerdpnative's header-generated bindings:

| RemoteDisplay (C++, FreeRDP 1.x)                                 | This port (FreeRDP 3)                                                                                                                                           |
|------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `freerdp_new` + `freerdp_context_new`                            | `freerdp_client_context_new`; no `PreConnect`/`PostConnect` overrides - `gdi_init` runs after `freerdp_connect`                                                 |
| `update->BitmapUpdate`, raw 16-bpp rectangles blitted by hand    | software GDI (`gdi_init`, BGRX32); every codec, RFX, NSCodec, planar, GFX, H.264 - lands in `rdpGdi.primary_buffer`, snapshotted between event-loop iterations |
| (no dynamic channels in FreeRDP 1.x)                           | opt-in (`REMOTEDISPLAY_GFX`): `ChannelConnected` events attach the Graphics Pipeline and video channels to the GDI                                              |
| `pointer_cache_register_callbacks` + `graphics_register_pointer` | same, with the generated `rdpPointer` struct                                                                                                                    |
| hand-decoded XOR/AND cursor masks                                | `freerdp_image_copy_from_pointer_data` → BGRA32 → `QCursor`                                                                                                     |
| `freerdp_get_fds` / `select` / `freerdp_check_fds`               | `freerdp_get_event_handles` / `WaitForMultipleObjects` / `freerdp_check_event_handles`                                                                          |
| `freerdp_keyboard_get_rdp_scancode_from_x11_keycode`             | same on X11; Windows scancodes direct (E0 → `KBDEXT`); macOS via `GetVirtualKeyCodeFromKeycode(…, APPLE)`                                                       |
| `freerdp_channels_*`, `freerdp_client_load_addins`               | done by `freerdp_connect`; rdpsnd via `FreeRDP_AudioPlayback`                                                                                                   |

Files map 1:1: `freerdpclient.py`, `freerdpeventloop.py`, `remotedisplaywidget.py`,
`screenbuffer.py` (the three `*ScreenBuffer` classes), `cursorchangenotifier.py`,
`rdpqtsoundplugin.py`; `clipboard.py` is new.

### Sound:

`rdpqtsoundplugin.py` ports `RdpQtSoundPlugin`: an rdpsnd device plugin on
Qt Multimedia (`QAudioOutput` on Qt 5, `QAudioSink` on Qt 6), installed
through `freerdp_register_addin_provider()` and selected with the static
channel `rdpsnd sys:qt`: the same mechanism as the C++ `WITH_QTSOUND` build.
It is used automatically when QtMultimedia is importable; otherwise, or with
`client.use_qt_sound = False`, FreeRDP's native rdpsnd backend (ALSA /
PulseAudio / WinMM / CoreAudio) plays instead. The provider is registered
after every `freerdp_client_context_new()`, because that call installs
FreeRDP's default provider and would otherwise override ours on reconnect.

### Clipboard:

`clipboard.py` implements the client side of the `cliprdr` channel
(MS-RDPECLIP) for text: a local copy announces `CF_UNICODETEXT`/`CF_TEXT`
to the server and answers its data request with UTF-16LE (CRLF line
endings); a remote copy is requested as soon as the server announces it and
placed on the Qt clipboard. All channel traffic runs on the RDP thread; the
GUI-only `QClipboard` is reached through queued signals. Files and images
are not transferred.

### Added:

- `setCredentials(user, password, domain="")`: the original had no
  authentication callback; real servers need one.
- `connectionFailed(str)`, `disconnectFromHost()`, mouse wheel, middle button.

## Platform notes:

- **Windows:** `WaitForMultipleObjects` is the Win32 API (WinPR only ships
  its own on other platforms); `FreeRdpEventLoop` takes it from `kernel32`
  there. Key events use Qt's native scancodes directly.
- **Linux:** keycodes (evdev + 8, under X11 and Wayland alike) go through
  WinPR's evdev table; FreeRDP's xkb table is the fallback.
- **All platforms:** the whole session - and therefore every FreeRDP call,
  including input - runs on the worker thread. `run()` is started from
  `QThread.started` (not a queued signal, which would run it on the GUI
  thread), and input slots are connected inside that handler with explicit
  `QueuedConnection`, so `freerdp_input_send_*` is only ever called from the
  RDP thread. A concurrent call from the GUI thread corrupts the transport;
  the Windows key, which floods the screen the instant it is pressed, was a
  reliable way to hit that race before this was fixed.
- **macOS:** Apple keycodes go through `GetVirtualKeyCodeFromKeycode`.
- Exceptions inside the session are reported through `connectionFailed(str)`;
  none propagate out of Qt slots (PyQt5 aborts the process on those).
- Every FreeRDP callback is wrapped by `@guard(...)`: FreeRDP invokes them
  from `freerdp_check_event_handles()`, and an exception escaping into C
  aborts the process - which looks like a crash *inside*
  `check_event_handles`. The guard logs the traceback once per callback and
  returns a safe value. Set `REMOTEDISPLAY_DEBUG=1` to trace every callback
  entry, which pinpoints a faulting one immediately.

## Feature switches and stability:

By default the session is set up **exactly like `examples/screenshot.py`**
of pyfreerdpnative, which is known to work against real Windows hosts:

    freerdp_client_context_new -> settings -> freerdp_connect -> gdi_init -> pump

In that configuration the only FreeRDP callbacks are the channel-connected
handler (once per channel, needed for the clipboard) and, if enabled, the
pointer callbacks; nothing else is registered inside FreeRDP: FreeRDP's own defaults decide codecs, channels
and the Graphics Pipeline; frames are snapshotted from the GDI framebuffer
on FreeRDP's own thread; input goes out through `freerdp_input_send_*`.
Everything that requires FreeRDP to call back into Python is opt-in:

| variable (or `FreeRdpClient` attribute) | default | adds                                                                                       |
|-----------------------------------------|---------|--------------------------------------------------------------------------------------------|
| `REMOTEDISPLAY_REMOTE_CURSOR`           | **on**  | pointer callbacks - remote cursor shape, hidden cursor and default arrow follow the server |
| `REMOTEDISPLAY_CLIPBOARD`               | **on**  | text clipboard both ways over the `cliprdr` channel                                        |
| `REMOTEDISPLAY_GFX`                     | off     | Graphics Pipeline forced on; rdpgfx attached to the GDI when its channel connects          |
| `REMOTEDISPLAY_H264`                    | off     | with GFX: allow AVC420/AVC444 (the `[experimental]` decoders of a media build)             |
| `REMOTEDISPLAY_QT_SOUND`                | off     | the Qt Multimedia rdpsnd device plugin (otherwise FreeRDP's own backend, per its defaults) |
| `REMOTEDISPLAY_DEBUG`                   | off     | trace every FreeRDP callback entry                                                         |

Enable one at a time. If the default configuration misbehaves while
`examples/screenshot.py` works against the same host, the difference is in
the widget and worth a bug report with `REMOTEDISPLAY_DEBUG=1` output.

## Threading:

Exactly as in the original: `FreeRdpClient` lives on a `QThread`; FreeRDP's
callbacks run there and cross to the GUI thread via queued signals. Frames are
**snapshotted on the RDP thread** by `FreeRdpEventLoop` after each
`freerdp_check_event_handles()`, at a steady `FRAMERATE_LIMIT` (40) per
second: the capture reports how long until its next deadline and the loop's
wait wakes exactly then, so the frame rate does not depend on when network
events happen to arrive. There is no second repaint timer in the widget -
each rectangle calls `update(rect)` and Qt coalesces them. The
snapshot goes into a preallocated buffer (`memmove`, 0.7 ms at 1080p), is
compared with the previous frame by `memcmp` and then in bands of
`BAND_ROWS` (16) rows; only changed bands cross to the GUI thread. There
they are composed into a widget-owned image **and scaled individually into
a cached view-sized pixmap** (0.3 ms per band), so painting is a blit.
Small bands are scaled smoothly, large ones (scrolling, video) with the
fast filter. All bands of a frame travel in one queued signal and cause one repaint;
when the view is scaled, bands are scaled straight from the incoming pixels
into the cache (the full-resolution image is only rebuilt on a view resize).
Measured at 1920x1080 with 300 rows changing every frame into a 720p view:
**129 fps uncapped (7.7 ms/frame)**; the default `FRAMERATE_LIMIT` of 40
uses under a third of that, leaving the GUI thread responsive. Raise it to
60 if the host sends that much.

Two pitfalls the implementation deliberately avoids, because each one alone
caps the widget at a few fps: allocating the 8 MB frame buffer every frame
(`ctypes.string_at`: ~30 ms of page faults), and comparing `memoryview`s of
`bytes` (CPython compares them element by element: ~500 ms per frame). the GUI
thread never reads the GDI framebuffer, which the Graphics Pipeline
reallocates on every `ResetGraphics`. No `EndPaint` hook is installed.
`FreeRdpEventLoop.exec()` blocks that thread for the whole session, so - as
in the C++ - it calls `QCoreApplication.processEvents()` each iteration to
keep queued slots on the client object (e.g. a queued `requestStop()`)
working.
`QCursor` objects are only ever created on the GUI thread. Repaints are
coalesced to at most `FRAMERATE_LIMIT` (40) per second.

One ctypes rule the port depends on: every callback handed to C is kept in
`FreeRdpClient._refs`. ctypes does not keep them alive, and a collected
callback is a crash on the next call from FreeRDP.

## License:

Same as the original RemoteDisplay: see [LICENSE](LICENSE).
