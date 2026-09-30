TEMPLATE = app
TARGET = naftamon
QT += widgets network multimedia
CONFIG += c++17 release
QMAKE_CXXFLAGS_RELEASE += -O2
SOURCES += src/main.cpp src/core.cpp src/thruk.cpp src/ui.cpp
HEADERS += src/core.h src/thruk.h src/ui.h
DESTDIR = .
OBJECTS_DIR = obj
MOC_DIR = obj
# version shown in the app and used by "Check for updates": the last commit that changed src/, so
# README or test commits do not offer an update (install.sh passes NAFTAMON_COMMIT when building
# from a downloaded archive without .git)
isEmpty(NAFTAMON_COMMIT): NAFTAMON_COMMIT = $$system(git -C $$PWD log -1 --abbrev=12 --format=%h -- src 2>/dev/null)
isEmpty(NAFTAMON_COMMIT): NAFTAMON_COMMIT = unknown
DEFINES += NAFTAMON_COMMIT=\\\"$$NAFTAMON_COMMIT\\\"
