#include "keyring.h"
#include <QDBusArgument>
#include <QDBusConnection>
#include <QDBusMessage>
#include <QDBusMetaType>
#include <QDBusObjectPath>
#include <QDBusVariant>
#include <QEventLoop>
#include <QTimer>

static const char SERVICE[] = "org.freedesktop.secrets";
static const char SERVICE_IF[] = "org.freedesktop.Secret.Service";
static const char DEFAULT_COLLECTION[] = "/org/freedesktop/secrets/aliases/default";
static const int CALL_TIMEOUT_MS = 3000;      // a missing service answers at once, this is for a hung one
static const int PROMPT_TIMEOUT_MS = 120000;  // the user typing the keyring password

using Attrs = QMap<QString, QString>;
using Paths = QList<QDBusObjectPath>;
struct Secret {  // D-Bus (oayays)
    QDBusObjectPath session;
    QByteArray params, value;
    QString contentType;
};
Q_DECLARE_METATYPE(Secret)

static QDBusArgument &operator<<(QDBusArgument &a, const Secret &s) {
    a.beginStructure();
    a << s.session << s.params << s.value << s.contentType;
    a.endStructure();
    return a;
}
static const QDBusArgument &operator>>(const QDBusArgument &a, Secret &s) {
    a.beginStructure();
    a >> s.session >> s.params >> s.value >> s.contentType;
    a.endStructure();
    return a;
}

namespace {
class PromptWait : public QObject {  // Secret.Prompt "Completed" signal
    Q_OBJECT
public:
    QEventLoop loop;
    bool dismissed = true;
public slots:
    void completed(bool d, const QDBusVariant &) { dismissed = d; loop.quit(); }
};
}

// true if the reply is usable, else *error gets the reason
static bool ok(const QDBusMessage &r, QString *error) {
    if (r.type() != QDBusMessage::ErrorMessage) return true;
    *error = r.errorName() == "org.freedesktop.DBus.Error.ServiceUnknown" ? "no keyring service is running"
                                                                           : r.errorMessage();
    return false;
}

static QDBusMessage call(const QString &path, const char *iface, const char *method, const QVariantList &args) {
    QDBusMessage m = QDBusMessage::createMethodCall(SERVICE, path, iface, method);
    m.setArguments(args);
    return QDBusConnection::sessionBus().call(m, QDBus::Block, CALL_TIMEOUT_MS);
}

// the keyring asks the user (unlock, confirm); "/" = nothing to ask
static bool runPrompt(const QDBusObjectPath &prompt, QString *error) {
    if (prompt.path() == "/") return true;
    PromptWait w;
    QDBusConnection::sessionBus().connect(SERVICE, prompt.path(), "org.freedesktop.Secret.Prompt", "Completed", &w,
                                          SLOT(completed(bool, QDBusVariant)));
    if (!ok(call(prompt.path(), "org.freedesktop.Secret.Prompt", "Prompt", {QString()}), error)) return false;
    QTimer::singleShot(PROMPT_TIMEOUT_MS, &w.loop, &QEventLoop::quit);
    w.loop.exec();
    if (w.dismissed) *error = "the keyring is locked";
    return !w.dismissed;
}

static bool unlock(const Paths &objects, QString *error) {
    QDBusMessage r = call("/org/freedesktop/secrets", SERVICE_IF, "Unlock", {QVariant::fromValue(objects)});
    return ok(r, error) && runPrompt(qdbus_cast<QDBusObjectPath>(r.arguments().value(1)), error);
}

// Session with the "plain" algorithm: the secret crosses the session bus unencrypted, which any
// process that may talk to the keyring could read anyway.
static bool session(QDBusObjectPath *out, QString *error) {
    static QDBusObjectPath s;
    if (s.path().isEmpty()) {
        if (!QDBusConnection::sessionBus().isConnected()) {
            *error = "no D-Bus session";
            return false;
        }
        qDBusRegisterMetaType<Secret>();
        qDBusRegisterMetaType<Attrs>();
        QDBusMessage r = call("/org/freedesktop/secrets", SERVICE_IF, "OpenSession",
                              {QString("plain"), QVariant::fromValue(QDBusVariant(QString()))});
        if (!ok(r, error)) return false;
        s = qdbus_cast<QDBusObjectPath>(r.arguments().value(1));
    }
    *out = s;
    return true;
}

static Attrs attrs(const QString &account) { return {{"application", "naftamon"}, {"account", account}}; }

// items of this account, unlocked if needed
static bool find(const QString &account, Paths *items, QString *error) {
    QDBusMessage r = call("/org/freedesktop/secrets", SERVICE_IF, "SearchItems", {QVariant::fromValue(attrs(account))});
    if (!ok(r, error)) return false;
    *items = qdbus_cast<Paths>(r.arguments().value(0));
    const Paths locked = qdbus_cast<Paths>(r.arguments().value(1));
    if (!items->isEmpty() || locked.isEmpty()) return true;
    *items = locked;
    return unlock(locked, error);
}

bool Keyring::get(const QString &account, QString *password, QString *error) {
    QDBusObjectPath s;
    Paths items;
    if (!session(&s, error) || !find(account, &items, error)) return false;
    password->clear();
    if (items.isEmpty()) return true;
    QDBusMessage r = call(items[0].path(), "org.freedesktop.Secret.Item", "GetSecret", {QVariant::fromValue(s)});
    if (!ok(r, error)) return false;
    *password = QString::fromUtf8(qdbus_cast<Secret>(r.arguments().value(0)).value);
    return true;
}

bool Keyring::set(const QString &account, const QString &password, QString *error) {
    QDBusObjectPath s;
    if (!session(&s, error) || !unlock({QDBusObjectPath(DEFAULT_COLLECTION)}, error)) return false;
    QVariantMap props{{"org.freedesktop.Secret.Item.Label", "Naftamon: " + account},
                      {"org.freedesktop.Secret.Item.Attributes", QVariant::fromValue(attrs(account))}};
    Secret secret{s, {}, password.toUtf8(), "text/plain; charset=utf8"};
    QDBusMessage r = call(DEFAULT_COLLECTION, "org.freedesktop.Secret.Collection", "CreateItem",
                          {props, QVariant::fromValue(secret), true});  // true: replace the old entry
    return ok(r, error) && runPrompt(qdbus_cast<QDBusObjectPath>(r.arguments().value(1)), error);
}

void Keyring::remove(const QString &account) {
    QDBusObjectPath s;
    Paths items;
    QString error;
    if (!session(&s, &error) || !find(account, &items, &error)) return;
    for (const QDBusObjectPath &i : items) {
        QDBusMessage r = call(i.path(), "org.freedesktop.Secret.Item", "Delete", {});
        if (ok(r, &error)) runPrompt(qdbus_cast<QDBusObjectPath>(r.arguments().value(0)), &error);
    }
}

#include "keyring.moc"
