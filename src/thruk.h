// Thruk client: same CGI endpoints as Nagstamon's Thruk server type, plus fast follow-up after commands.
#pragma once
#include "core.h"
#include <QObject>
#include <QNetworkAccessManager>
#include <QHash>
#include <QTimer>
#include <QUrlQuery>
#include <functional>

class QNetworkReply;

struct ServerConf {
    QString name, url, user, password;  // url: monitor CGI URL, e.g. http://host/thruk/cgi-bin
    QString disabledBackends;           // comma separated backend ids (Nagstamon disabled_backends)
    bool enabled = true, ignoreTls = false, useDisplayNameService = false;
    QString cgiUrl() const;
};

// Values scraped from a cmd.cgi form page, like Nagstamon does for start_time/end_time.
struct CmdForm { QString startTime, endTime, token; };

class ThrukServer : public QObject {
    Q_OBJECT
public:
    explicit ThrukServer(const ServerConf &conf, QObject *parent = nullptr);
    const ServerConf conf;

    RawStatus raw;          // last successful poll
    QString error;          // empty if last poll succeeded
    qint64 lastRefreshMs = 0;  // duration of last poll
    bool hasData = false;

    void refresh();
    void recheck(const Item &item);
    // SCHEDULE_FORCED_HOST_SVC_CHECKS; services = the host's services we list, followed up like rechecks
    void recheckHostServices(const Item &item, const QVector<Item> &services);
    // expireEpoch 0 = no expiry; only honoured by cores with expiring acks (Naemon, Icinga)
    void acknowledge(const Item &item, const QString &comment, bool sticky, bool notify, bool persistent,
                     qint64 expireEpoch = 0, const QVector<Item> &alsoServices = {});
    void removeAcknowledgement(const Item &item);
    void downtime(const Item &item, const QString &comment, bool fixed, const QString &start,
                  const QString &end, int hours, int minutes);
    void submitResult(const Item &item, int state, const QString &output, const QString &perfdata);
    void fetchForm(const Item &item, int cmdTyp, std::function<void(CmdForm)> done);
    QUrl monitorUrl(const Item &item) const;
    bool isRechecking(const QString &itemKey) const { return pending.contains(itemKey); }

signals:
    void updated();                        // new data or new error
    void commandFailed(const QString &msg);
    void recheckingChanged();

private:
    QNetworkAccessManager nam;
    bool loggedIn = false, busy = false, again = false, reloginTried = false;
    QString token;  // CSRF token of our session
    // rechecks waiting for a newer last_check; polled with small per-host queries
    struct Pending { Item item; qint64 deadlineMs = 0, doneAtMs = 0; };
    QHash<QString, Pending> pending;  // item key ->
    QTimer followTimer;
    int followInFlight = 0;

    QNetworkRequest request(const QString &url, int timeoutMs) const;
    QNetworkReply *get(const QString &url);
    QNetworkReply *post(const QString &url, const QByteArray &body);
    void login(std::function<void(bool)> done);
    void finishPoll(QNetworkReply *hosts, QNetworkReply *services, qint64 started);
    void sendCmd(const Item &item, int cmdTyp, QList<QPair<QString, QString>> params,
                 std::function<void()> done = {});
    void addPending(const Item &item);
    void followUp();
    void withToken(const Item &item, int cmdTyp, std::function<void()> fn);
};

QByteArray formEncode(const QList<QPair<QString, QString>> &params);
CmdForm parseCmdForm(const QString &html);
bool parseStatusJson(const QByteArray &json, bool isHost, const QString &server, bool displayName,
                     QVector<Item> *out);
