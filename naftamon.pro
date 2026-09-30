TEMPLATE = app
TARGET = naftamon
QT += widgets network multimedia dbus
CONFIG += c++17 release
QMAKE_CXXFLAGS_RELEASE += -O2
SOURCES += src/main.cpp src/core.cpp src/thruk.cpp src/keyring.cpp src/ui.cpp
HEADERS += src/core.h src/thruk.h src/keyring.h src/ui.h
DESTDIR = .
OBJECTS_DIR = obj
MOC_DIR = obj
# version shown in the app and used by "Check for updates": git commit of the sources
# (install.sh passes NAFTAMON_COMMIT when building from a downloaded archive without .git)
isEmpty(NAFTAMON_COMMIT): NAFTAMON_COMMIT = $$system(git -C $$PWD rev-parse --short=12 HEAD 2>/dev/null)
isEmpty(NAFTAMON_COMMIT): NAFTAMON_COMMIT = unknown
DEFINES += NAFTAMON_COMMIT=\\\"$$NAFTAMON_COMMIT\\\"
