// Passwords in the desktop keyring: freedesktop Secret Service over D-Bus (GNOME Keyring, KWallet).
#pragma once
#include <QString>

namespace Keyring {
// false = keyring not usable, *error says why. A missing entry is not an error (empty password).
bool get(const QString &account, QString *password, QString *error);
bool set(const QString &account, const QString &password, QString *error);
void remove(const QString &account);
}
