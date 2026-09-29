#include "thruk.h"
#include <QNetworkReply>
#include <QNetworkCookie>
#include <QNetworkCookieJar>
#include <QAuthenticator>
#include <QJsonDocument>
#include <QJsonArray>
#include <QJsonObject>
#include <QDateTime>
#include <QTimer>
#include <memory>

// Same query strings as Nagstamon's Thruk.py (host section 5 fixed: Nagstamon has a
// "dfl_s5_hostprop" typo, which makes Thruk return every host).
static const char HOSTS_QUERY[] =
    "/status.cgi?hostgroup=all&style=hostdetail&dfl_s0_hoststatustypes=12&dfl_s1_hostprops=1"
    "&dfl_s2_hostprops=4&dfl_s3_hostprops=524288&dfl_s4_hostprops=4096&dfl_s5_hostprops=16"
    "&view_mode=json&entries=all&columns=name,state,last_check,last_state_change,plugin_output,"
    "current_attempt,max_check_attempts,active_checks_enabled,notifications_enabled,is_flapping,"
    "acknowledged,scheduled_downtime_depth,state_type,host_display_name,display_name";
static const char SERVICES_QUERY[] =
    "/status.cgi?host=all&servicestatustypes=28&view_mode=json&entries=all&columns=host_name,"
    "description,state,last_check,last_state_change,plugin_output,current_attempt,max_check_attempts,"
    "active_checks_enabled,is_flapping,notifications_enabled,acknowledged,state_type,"
    "scheduled_downtime_depth,host_display_name,display_name";

static const int POLL_TIMEOUT_MS = 20000;
static const int CMD_TIMEOUT_MS = 30000;      // Thruk may hold a command up to wait_timeout (default 10s)
static const int RECHECK_FOLLOW_MS = 30000;   // give up following a recheck after this
static const int RECHECK_FOLLOW_STEP_MS = 250;  // per-host queries are tiny, so poll fast

// url is Nagstamon's "Monitor CGI URL" (http://host/thruk/cgi-bin); a bare .../thruk also works
QString ServerConf::cgiUrl() const {
    QString u = url.trimmed();
    while (u.endsWith('/')) u.chop(1);
    return u.endsWith("/cgi-bin") ? u : u + "/cgi-bin";
}

QByteArray formEncode(const QList<QPair<QString, QString>> &params) {
    QByteArray out;
    for (const auto &p : params) {
        if (!out.isEmpty()) out += '&';
        out += QUrl::toPercentEncoding(p.first) + '=' + QUrl::toPercentEncoding(p.second);
    }
    return out;
}

static QString inputValue(const QString &html, const QString &name) {
    QRegularExpression tag("<input[^>]*name=[\"']" + QRegularExpression::escape(name) + "[\"'][^>]*>",
                           QRegularExpression::CaseInsensitiveOption);
    QRegularExpressionMatch m = tag.match(html);
    if (!m.hasMatch()) return {};
    static const QRegularExpression val("value=[\"']([^\"']*)[\"']", QRegularExpression::CaseInsensitiveOption);
    return val.match(m.captured(0)).captured(1);
}

CmdForm parseCmdForm(const QString &html) {
    CmdForm f{inputValue(html, "start_time"), inputValue(html, "end_time"), inputValue(html, "CSRFtoken")};
    if (f.token.isEmpty()) {  // also exposed to JS as: var CSRFtoken = '...';
        static const QRegularExpression js("CSRFtoken\\s*=\\s*['\"]([^'\"]+)['\"]");
        f.token = js.match(html).captured(1);
    }
    return f;
}

static qint64 num(const QJsonValue &v) { return v.toVariant().toLongLong(); }

bool parseStatusJson(const QByteArray &json, bool isHost, const QString &server, bool displayName,
                     QVector<Item> *out) {
    QJsonParseError err;
    QJsonDocument doc = QJsonDocument::fromJson(json, &err);
    if (err.error != QJsonParseError::NoError || !doc.isArray()) return false;
    const QJsonArray arr = doc.array();
    out->reserve(arr.size());
    for (const auto &v : arr) {
        QJsonObject o = v.toObject();
        Item i;
        i.server = server;
        int st = int(num(o["state"]));
        if (isHost) {
            i.host = o["name"].toString();
            i.state = st == 1 ? DOWN : st == 2 ? UNREACHABLE : UP;
        } else {
            i.host = o["host_name"].toString();
            i.realService = o["description"].toString();
            i.service = displayName ? o["display_name"].toString() : i.realService;
            i.state = st == 1 ? WARNING : st == 2 ? CRITICAL : st == 3 ? UNKNOWN : UP;
            if (i.state == UP) continue;  // servicestatustypes=28 never returns OK, be safe anyway
        }
        i.hard = num(o["state_type"]) == 1;
        i.lastCheck = num(o["last_check"]);
        i.lastChange = num(o["last_state_change"]);
        i.attempt = int(num(o["current_attempt"]));
        i.maxAttempts = int(num(o["max_check_attempts"]));
        i.output = o["plugin_output"].toString().replace('\n', ' ').trimmed();
        i.passive = num(o["active_checks_enabled"]) == 0;
        i.notifDisabled = num(o["notifications_enabled"]) == 0;
        i.flapping = num(o["is_flapping"]) != 0;  // Nagstamon uses notifications_enabled here for services (bug)
        i.ack = num(o["acknowledged"]) != 0;
        i.downtime = num(o["scheduled_downtime_depth"]) != 0;
        out->append(i);
    }
    return true;
}

ThrukServer::ThrukServer(const ServerConf &c, QObject *parent) : QObject(parent), conf(c) {
    nam.setAutoDeleteReplies(false);
    connect(&nam, &QNetworkAccessManager::authenticationRequired, this,
            [this](QNetworkReply *reply, QAuthenticator *auth) {
                // HTTP basic auth in front of Thruk; give credentials once per request, else fail
                if (reply->property("authTried").toBool()) return;
                reply->setProperty("authTried", true);
                auth->setUser(conf.user);
                auth->setPassword(conf.password);
            });
    connect(&followTimer, &QTimer::timeout, this, &ThrukServer::followUp);
    if (conf.ignoreTls)
        connect(&nam, &QNetworkAccessManager::sslErrors, this,
                [](QNetworkReply *r, const QList<QSslError> &) { r->ignoreSslErrors(); });
}

// Qt's text for HTTP 401/403 ("Host requires authentication") hides the cause
static QString replyError(QNetworkReply *r) {
    int code = r->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
    if (code == 401 || code == 403)
        return QString("Login failed (HTTP %1): check username/password").arg(code);
    return r->errorString();
}

QNetworkRequest ThrukServer::request(const QString &url, int timeoutMs) const {
    QNetworkRequest req{QUrl(url)};
    req.setTransferTimeout(timeoutMs);
    req.setHeader(QNetworkRequest::UserAgentHeader, "naftamon");
    // never follow a redirect to another host: the Authorization header would go with it
    req.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::SameOriginRedirectPolicy);
    // Like Nagstamon's default (authentication = 'basic'): send Basic auth on every request instead
    // of waiting for a challenge; some setups answer 401 without a usable Basic challenge.
    if (!conf.user.isEmpty())
        req.setRawHeader("Authorization",
                         "Basic " + (conf.user.toUtf8() + ':' + conf.password.toUtf8()).toBase64());
    return req;
}

QNetworkReply *ThrukServer::get(const QString &url) {
    return nam.get(request(url, POLL_TIMEOUT_MS));
}

QNetworkReply *ThrukServer::post(const QString &url, const QByteArray &body) {
    QNetworkRequest req = request(url, CMD_TIMEOUT_MS);
    req.setHeader(QNetworkRequest::ContentTypeHeader, "application/x-www-form-urlencoded; charset=UTF-8");
    return nam.post(req, body);
}

void ThrukServer::login(std::function<void(bool)> done) {
    QUrl base(conf.cgiUrl() + "/");
    if (!conf.disabledBackends.isEmpty()) {
        QStringList parts;
        for (const QString &b : conf.disabledBackends.split(',', Qt::SkipEmptyParts)) parts << b.trimmed() + "=2";
        QNetworkCookie c("thruk_backends", parts.join('&').toUtf8());
        nam.cookieJar()->setCookiesFromUrl({c}, base);
    }
    if (conf.user.isEmpty()) {  // no credentials: anonymous or reverse-proxy auth
        loggedIn = true;
        return done(true);
    }
    nam.cookieJar()->setCookiesFromUrl({QNetworkCookie("thruk_test", "***")}, base);
    QNetworkReply *r = post(conf.cgiUrl() + "/login.cgi?",
                            formEncode({{"login", conf.user}, {"password", conf.password}, {"submit", "Login"}}));
    // Like Nagstamon: HTTP success counts as logged in; a login page instead of JSON on the
    // next poll triggers one re-login and then "Login failed".
    connect(r, &QNetworkReply::finished, this, [this, r, done] {
        r->deleteLater();
        loggedIn = r->error() == QNetworkReply::NoError;
        if (!loggedIn) error = replyError(r);
        done(loggedIn);
    });
}

void ThrukServer::refresh() {
    if (!conf.enabled) return;
    if (busy) { again = true; return; }
    busy = true;
    if (!loggedIn) {
        login([this](bool ok) {
            busy = false;
            if (ok) return refresh();
            again = false;
            emit updated();
        });
        return;
    }
    qint64 t0 = QDateTime::currentMSecsSinceEpoch();
    // hosts and services in parallel (Nagstamon fetches them one after the other)
    QNetworkReply *h = get(conf.cgiUrl() + HOSTS_QUERY);
    QNetworkReply *s = get(conf.cgiUrl() + SERVICES_QUERY);
    auto left = std::make_shared<int>(2);
    auto fin = [=] { if (--*left == 0) finishPoll(h, s, t0); };
    connect(h, &QNetworkReply::finished, this, fin);
    connect(s, &QNetworkReply::finished, this, fin);
}

void ThrukServer::finishPoll(QNetworkReply *h, QNetworkReply *s, qint64 started) {
    h->deleteLater();
    s->deleteLater();
    busy = false;
    QByteArray hb = h->readAll(), sb = s->readAll();
    lastRefreshMs = QDateTime::currentMSecsSinceEpoch() - started;
    auto done = [this] {
        emit updated();
        if (again) { again = false; refresh(); }
    };

    for (QNetworkReply *r : {h, s}) {
        int code = r->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        if (code == 401 || code == 403) loggedIn = false;
        if (r->error() != QNetworkReply::NoError) {
            error = replyError(r);
            return done();
        }
    }
    // HTML instead of JSON: session expired / login page; log in again once
    if (hb.trimmed().startsWith('<') || sb.trimmed().startsWith('<')) {
        loggedIn = false;
        if (!reloginTried) {
            reloginTried = true;
            again = true;
        } else {
            error = "Login failed";
        }
        return done();
    }
    RawStatus fresh;
    if (!parseStatusJson(hb, true, conf.name, false, &fresh.hosts) ||
        !parseStatusJson(sb, false, conf.name, conf.useDisplayNameService, &fresh.services)) {
        error = "Invalid JSON from Thruk";
        return done();
    }
    raw = std::move(fresh);
    error.clear();
    hasData = true;
    reloginTried = false;
    // drop "rechecking" marks whose result is in this data: newer last_check, recovered (gone),
    // or seen done by followUp() before this poll started
    QHash<QString, qint64> seen;
    for (const auto *list : {&raw.hosts, &raw.services})
        for (const Item &i : *list) seen.insert(i.key(), i.lastCheck);
    for (auto it = pending.begin(); it != pending.end();) {
        auto lc = seen.find(it.key());
        bool resolved = lc == seen.end() || lc.value() > it->item.lastCheck || (it->doneAtMs && it->doneAtMs <= started);
        it = resolved ? pending.erase(it) : std::next(it);
    }
    if (pending.isEmpty()) followTimer.stop();
    done();
}

// After a recheck: poll only the affected hosts every second (status.cgi?host=H, a few rows)
// until each rechecked item has a newer last_check, then run one full refresh. Works whether
// or not Thruk's wait feature is on; the "rechecking" mark stays until the refresh shows the result.
void ThrukServer::addPending(const Item &item) {
    pending.insert(item.key(), {item, QDateTime::currentMSecsSinceEpoch() + RECHECK_FOLLOW_MS, 0});
    if (!followTimer.isActive()) followTimer.start(RECHECK_FOLLOW_STEP_MS);
    emit recheckingChanged();
}

void ThrukServer::followUp() {
    qint64 now = QDateTime::currentMSecsSinceEpoch();
    bool changed = false;
    for (auto it = pending.begin(); it != pending.end();) {
        if (now > it->deadlineMs) { it = pending.erase(it); changed = true; }
        else ++it;
    }
    QSet<QString> hostQuery, serviceQuery;
    for (const Pending &p : pending)
        if (!p.doneAtMs) (p.item.isHost() ? hostQuery : serviceQuery).insert(p.item.host);
    if (pending.isEmpty()) followTimer.stop();
    if (changed) emit recheckingChanged();
    if (followInFlight > 0 || !loggedIn) return;  // previous round still running

    auto query = [this](const QString &host, bool hosts) {
        QString url = conf.cgiUrl() + "/status.cgi?" + formEncode({{"host", host}}) +
                      (hosts ? "&style=hostdetail&view_mode=json&columns=name,last_check"
                             : "&view_mode=json&columns=description,last_check");
        ++followInFlight;
        QNetworkReply *r = get(url);
        connect(r, &QNetworkReply::finished, this, [this, r, host, hosts] {
            r->deleteLater();
            --followInFlight;
            QHash<QString, qint64> lastCheck;  // name/description -> last_check
            const QJsonArray rows = QJsonDocument::fromJson(r->readAll()).array();
            if (r->error() != QNetworkReply::NoError || rows.isEmpty()) return;  // next tick retries
            for (const auto &v : rows) {
                QJsonObject o = v.toObject();
                lastCheck.insert(o[hosts ? "name" : "description"].toString(), num(o["last_check"]));
            }
            bool anyDone = false;
            for (Pending &p : pending) {
                if (p.doneAtMs || p.item.host != host || p.item.isHost() != hosts) continue;
                auto lc = lastCheck.find(hosts ? p.item.host : p.item.realService);
                if (lc == lastCheck.end() || lc.value() > p.item.lastCheck) {
                    p.doneAtMs = QDateTime::currentMSecsSinceEpoch();
                    anyDone = true;
                }
            }
            if (anyDone) refresh();
        });
    };
    for (const QString &h : hostQuery) query(h, true);
    for (const QString &h : serviceQuery) query(h, false);
}

void ThrukServer::fetchForm(const Item &item, int cmdTyp, std::function<void(CmdForm)> done) {
    auto go = [=] {
        QList<QPair<QString, QString>> q{{"cmd_typ", QString::number(cmdTyp)}, {"host", item.host}};
        if (!item.isHost()) q.append(QPair<QString, QString>{"service", item.realService});
        QNetworkReply *r = get(conf.cgiUrl() + "/cmd.cgi?" + formEncode(q));
        connect(r, &QNetworkReply::finished, this, [this, r, done] {
            r->deleteLater();
            if (r->error() != QNetworkReply::NoError) {
                emit commandFailed(conf.name + ": " + replyError(r));
                return;
            }
            CmdForm f = parseCmdForm(QString::fromUtf8(r->readAll()));
            if (!f.token.isEmpty()) token = f.token;
            done(f);
        });
    };
    if (loggedIn) return go();
    login([this, go](bool ok) {
        if (ok) go();
        else emit commandFailed(conf.name + ": " + error);
    });
}

void ThrukServer::sendCmd(const Item &item, int cmdTyp, QList<QPair<QString, QString>> params,
                          std::function<void()> done) {
    // Order matters for some cores (see Nagstamon comments): cmd_typ, cmd_mod, host, service first.
    QList<QPair<QString, QString>> p{{"cmd_typ", QString::number(cmdTyp)}, {"cmd_mod", "2"}, {"host", item.host}};
    if (!item.isHost()) p.append(QPair<QString, QString>{"service", item.realService});
    p += params;
    p.append(QPair<QString, QString>{"btnSubmit", "Commit"});
    p.append(QPair<QString, QString>{"CSRFtoken", token});
    // json=1 makes Thruk answer with JSON and, with use_wait_feature on, hold the reply until
    // the command has taken effect (recheck: until last_check >= now). Nagstamon does not send it.
    p.append(QPair<QString, QString>{"json", "1"});
    QNetworkReply *r = post(conf.cgiUrl() + "/cmd.cgi", formEncode(p));
    connect(r, &QNetworkReply::finished, this, [this, r, done] {
        r->deleteLater();
        QByteArray body = r->readAll();
        QJsonObject o = QJsonDocument::fromJson(body).object();
        if (r->error() != QNetworkReply::NoError)
            emit commandFailed(conf.name + ": " + replyError(r));
        else if (body.contains("possible csrf")) {
            token.clear();  // stale session token, next command fetches a new one
            emit commandFailed(conf.name + ": command rejected (CSRF token), try again");
        }
        else if (o.contains("success") && !o["success"].toVariant().toBool())
            emit commandFailed(conf.name + ": " + o["error"].toString());
        if (done) done();
        refresh();  // show the effect immediately instead of waiting for the next interval
    });
}

void ThrukServer::recheck(const Item &item) {
    if (item.passive) return;  // Nagstamon does not recheck passive-only checks
    addPending(item);
    int typ = item.isHost() ? 96 : 7;
    fetchForm(item, typ, [=](CmdForm f) {
        sendCmd(item, typ, {{"start_time", f.startTime}, {"force_check", "on"}});
    });
}

void ThrukServer::recheckHostServices(const Item &item, const QVector<Item> &services) {
    Item host = item;
    host.service.clear();
    host.realService.clear();
    for (const Item &s : services) addPending(s);
    fetchForm(host, 17, [=](CmdForm f) {
        sendCmd(host, 17, {{"start_time", f.startTime}, {"force_check", "on"}});
    });
}

void ThrukServer::removeAcknowledgement(const Item &item) {
    int typ = item.isHost() ? 51 : 52;
    withToken(item, typ, [=] { sendCmd(item, typ, {}); });
}

void ThrukServer::acknowledge(const Item &item, const QString &comment, bool sticky, bool notify, bool persistent,
                              qint64 expireEpoch, const QVector<Item> &alsoServices) {
    QList<QPair<QString, QString>> p{{"com_author", conf.user}, {"com_data", comment}};
    // flags must be absent when off, the Nagios core treats any value as "on"
    if (notify) p.append(QPair<QString, QString>{"send_notification", "on"});
    if (persistent) p.append(QPair<QString, QString>{"persistent", "on"});
    if (sticky) p.append(QPair<QString, QString>{"sticky_ack", "on"});
    if (expireEpoch > 0) {  // Thruk parses a unix timestamp, so no server timezone issue
        p.append(QPair<QString, QString>{"use_expire", "on"});
        p.append(QPair<QString, QString>{"expire_time", QString::number(expireEpoch)});
    }
    withToken(item, item.isHost() ? 33 : 34, [=] {
        sendCmd(item, item.isHost() ? 33 : 34, p);
        for (const Item &s : alsoServices) sendCmd(s, 34, p);
    });
}

void ThrukServer::downtime(const Item &item, const QString &comment, bool fixed, const QString &start,
                           const QString &end, int hours, int minutes) {
    int typ = item.isHost() ? 55 : 56;
    withToken(item, typ, [=] {
        sendCmd(item, typ, {{"trigger", "0"}, {"com_author", conf.user}, {"com_data", comment},
                            {"fixed", fixed ? "1" : "0"}, {"start_time", start}, {"end_time", end},
                            {"hours", QString::number(hours)}, {"minutes", QString::number(minutes)}});
    });
}

void ThrukServer::submitResult(const Item &item, int state, const QString &output, const QString &perfdata) {
    int typ = item.isHost() ? 87 : 30;
    withToken(item, typ, [=] {
        sendCmd(item, typ, {{"plugin_state", QString::number(state)}, {"plugin_output", output},
                            {"performance_data", perfdata}});
    });
}

void ThrukServer::withToken(const Item &item, int cmdTyp, std::function<void()> fn) {
    if (loggedIn && !token.isEmpty()) return fn();
    fetchForm(item, cmdTyp, [fn](CmdForm) { fn(); });
}

QUrl ThrukServer::monitorUrl(const Item &item) const {
    QList<QPair<QString, QString>> q{{"host", item.host}};
    if (!item.isHost()) q.append(QPair<QString, QString>{"service", item.realService});
    return QUrl(conf.cgiUrl() + "/extinfo.cgi?type=" + (item.isHost() ? "1&" : "2&") + formEncode(q));
}
