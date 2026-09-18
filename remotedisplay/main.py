# coding=utf-8
"""
python example/main.py <host> <port> <width> <height> [user] [password]
"""
from remotedisplay import RemoteDisplayWidget
from ManyQt.QtWidgets import QApplication
from sys import stderr, argv, exit


def main(a):
    app = QApplication(a)
    args = app.arguments()
    if len(args) < 5:
        stderr.write('Usage: main.py <host> <port> <width> <height> [user] [password]\n')
        return -1
    host, port, width, height = args[1], int(args[2]), int(args[3]), int(args[4])
    w = RemoteDisplayWidget()
    w.resize(width, height)
    w.setDesktopSize(width, height)
    if len(args) > 6:
        w.setCredentials(args[5], args[6])
    w.connectionFailed.connect(lambda msg: stderr.write('connection failed: %s\n' % msg))
    w.disconnected.connect(app.quit)
    w.connectToHost(host, port)
    w.show()
    return app.exec_() if hasattr(app, "exec_") else app.exec()


if __name__ == "__main__":
    exit(main(argv))
