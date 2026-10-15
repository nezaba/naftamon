TEMPLATE = app
TARGET = naftamon-tests
QT = core network widgets multimedia
CONFIG += c++17 console
SOURCES += test_main.cpp ../src/core.cpp ../src/thruk.cpp ../src/webhookgateway.cpp ../src/ui.cpp
HEADERS += ../src/core.h ../src/thruk.h ../src/webhookgateway.h ../src/ui.h
DEFINES += NAFTAMON_COMMIT=\\\"test\\\"
DESTDIR = .
OBJECTS_DIR = obj
MOC_DIR = obj
