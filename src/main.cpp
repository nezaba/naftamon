#include "ui.h"
#include <QApplication>
#include <QCryptographicHash>
#include <QLockFile>
#include <QStandardPaths>
#include <cstdio>

int main(int argc, char **argv) {
    QApplication app(argc, argv);
    app.setApplicationName("naftamon");
    app.setDesktopFileName("naftamon");
    app.setQuitOnLastWindowClosed(false);  // lives in the tray / status bar
    app.setWindowIcon(QIcon(appIconPixmap(256)));

    // naftamon --export-icon FILE.png [SIZE]: used by install.sh for the desktop entry
    QStringList args = app.arguments();
    if (args.size() >= 3 && args[1] == "--export-icon")
        return appIconPixmap(args.size() > 3 ? args[3].toInt() : 256).save(args[2]) ? 0 : 1;

    // one instance per config file: a second one would poll and notify twice
    QString id = QCryptographicHash::hash(AppConfig::path().toUtf8(), QCryptographicHash::Md5).toHex().left(8);
    QLockFile lock(QStandardPaths::writableLocation(QStandardPaths::RuntimeLocation) + "/naftamon-" + id + ".lock");
    lock.setStaleLockTime(0);  // held for the whole run; a crashed owner is detected by its PID
    if (!lock.tryLock()) {
        std::fprintf(stderr, "naftamon is already running (see: pgrep -a naftamon)\n");
        return 1;
    }
    App a;
    return app.exec();
}
