#include "webhookgateway.h"
#include <QDateTime>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QNetworkReply>
#include <QNetworkRequest>

WebhookGateway::WebhookGateway(QObject *parent, int timeoutMs)
    : QObject(parent), timeoutMs_(qMax(1, timeoutMs)) {
    connect(&timer_, &QTimer::timeout, this, &WebhookGateway::sendNow);
}

void WebhookGateway::setServers(const QVector<ThrukServer *> &servers) {
    QVector<QPointer<ThrukServer>> next;
    next.reserve(servers.size());
    for (ThrukServer *server : servers) next.append(server);
    if (next == servers_) return;
    cancelInFlight();
    servers_ = std::move(next);
    if (enabled_) QTimer::singleShot(0, this, &WebhookGateway::sendNow);
}

void WebhookGateway::configure(bool enabled, const QString &url, int intervalSeconds) {
    const QUrl nextUrl(url.trimmed());
    const int nextInterval = qBound(1, intervalSeconds, 86400);
    const bool nextEnabled = enabled && isValidUrl(url);
    if (enabled_ == nextEnabled && url_ == nextUrl && intervalSeconds_ == nextInterval) {
        // Saving settings explicitly lets a receiver that was upgraded in place
        // be probed again for support of the extended snapshot fields.
        if (enabled_ && legacyPayloadMode_) {
            legacyPayloadMode_ = false;
            if (!inFlight_) QTimer::singleShot(0, this, &WebhookGateway::sendNow);
        }
        return;
    }
    cancelInFlight();
    timer_.stop();
    legacyPayloadMode_ = false;
    enabled_ = nextEnabled;
    url_ = nextUrl;
    intervalSeconds_ = nextInterval;
    if (enabled_) {
        timer_.start(intervalSeconds_ * 1000);
        QTimer::singleShot(0, this, &WebhookGateway::sendNow);
    }
}

bool WebhookGateway::isValidUrl(const QString &url) {
    const QUrl target(url.trimmed());
    return (target.scheme() == "http" || target.scheme() == "https") &&
           !target.host().isEmpty() && target.userInfo().isEmpty();
}

void WebhookGateway::cancelInFlight() {
    if (!inFlight_) return;
    QNetworkReply *reply = inFlight_;
    inFlight_ = nullptr;
    reply->abort();
    emit requestFinished(QDateTime::currentDateTime(), false, 0, "Cancelled", QString());
}

static QJsonObject itemJson(const Item &item) {
    auto utc = [](qint64 epoch) { return QDateTime::fromSecsSinceEpoch(epoch, Qt::UTC).toString(Qt::ISODate); };
    QJsonObject alert;
    alert["server"] = item.server;
    alert["backend"] = item.backend;
    alert["host"] = item.host;
    alert["service"] = item.service;
    alert["type"] = item.isHost() ? "host" : "service";
    alert["state"] = stateName(item.state);
    alert["status_information"] = item.output;
    alert["hard"] = item.hard;
    alert["acknowledged"] = item.ack;
    alert["downtime"] = item.downtime;
    alert["last_check"] = utc(item.lastCheck);
    alert["last_change"] = utc(item.lastChange);
    return alert;
}

QJsonObject WebhookGateway::snapshot() const {
    QJsonArray serverStatuses, alerts, okStatuses;
    bool complete = !servers_.isEmpty();
    for (const auto &server : servers_) {
        if (!server) { complete = false; continue; }
        const QString status = !server->error.isEmpty() ? "error" : server->hasData ? "ok" : "pending";
        serverStatuses.append(QJsonObject{{"server", server->conf.name}, {"status", status}});
        if (status != "ok") { complete = false; continue; }
        for (const auto *list : {&server->raw.hosts, &server->raw.services})
            for (const Item &item : *list)
                if (item.state != UP) alerts.append(itemJson(item));
        for (const Item &item : server->raw.okStatuses)
            if (item.state == UP) okStatuses.append(itemJson(item));
    }
    return {{"timestamp", QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs)},
            {"source", "NaftaMon"}, {"complete", complete},
            {"servers", serverStatuses}, {"alerts", alerts}, {"ok_statuses", okStatuses}};
}

QJsonObject WebhookGateway::legacyCompatiblePayload(QJsonObject payload) {
    payload.remove("ok_statuses");
    QJsonArray alerts = payload.value("alerts").toArray();
    for (qsizetype i = 0; i < alerts.size(); ++i) {
        QJsonObject alert = alerts.at(i).toObject();
        alert.remove("status_information");
        alerts.replace(i, alert);
    }
    payload["alerts"] = alerts;
    return payload;
}

void WebhookGateway::sendNow() {
    if (!enabled_ || inFlight_) return;
    QJsonObject payload = snapshot();
    const bool allowFallback = !legacyPayloadMode_;
    if (legacyPayloadMode_) payload = legacyCompatiblePayload(payload);
    post(url_, payload, false, allowFallback);
}

bool WebhookGateway::testWebhook(const QString &url) {
    if (!isValidUrl(url) || testInFlight_) return false;
    const QJsonObject payload{{"type", "test"}, {"test", true},
                              {"source", "NaftaMon"},
                              {"timestamp", QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs)},
                              {"message", "NaftaMon test webhook"}};
    return post(QUrl(url.trimmed()), payload, true);
}

bool WebhookGateway::post(const QUrl &url, const QJsonObject &payload, bool test,
                          bool allowCompatibilityFallback) {
    QPointer<QNetworkReply> &active = test ? testInFlight_ : inFlight_;
    if (active) return false;
    QNetworkRequest request(url);
    request.setHeader(QNetworkRequest::ContentTypeHeader, "application/json; charset=utf-8");
    request.setHeader(QNetworkRequest::UserAgentHeader, "NaftaMon WebhookGateway");
    request.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::ManualRedirectPolicy);
    request.setTransferTimeout(timeoutMs_);
    QNetworkReply *reply = network_.post(request, QJsonDocument(payload).toJson(QJsonDocument::Compact));
    active = reply;
    QTimer::singleShot(timeoutMs_, reply, [reply] { if (reply->isRunning()) reply->abort(); });
    connect(reply, &QNetworkReply::finished, this,
            [this, reply, test, url, payload, allowCompatibilityFallback] {
        reply->deleteLater();
        QPointer<QNetworkReply> &current = test ? testInFlight_ : inFlight_;
        if (current != reply) return;
        current = nullptr;
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        bool rejectedExtensions = false;
        if (!test && allowCompatibilityFallback && status == 422) {
            const QByteArray body = reply->readAll();
            rejectedExtensions = body.contains("ok_statuses") || body.contains("status_information");
        }
        if (rejectedExtensions) {
            // Older NaftaMon Web versions reject unknown fields. Retry the same
            // snapshot without additions and keep that shape for this destination.
            legacyPayloadMode_ = true;
            post(url, legacyCompatiblePayload(payload), false);
            return;
        }
        const bool success = reply->error() == QNetworkReply::NoError && status >= 200 && status < 300;
        const QString error = success ? QString() :
            reply->error() == QNetworkReply::NoError ? QString("HTTP %1").arg(status) : reply->errorString();
        QString response;
        if (test && reply->isReadable()) {
            response = QString::fromUtf8(reply->read(4096)).trimmed();
            if (reply->bytesAvailable() > 0) response += "… (truncated)";
        }
        emit requestFinished(QDateTime::currentDateTime(), test, status, error, response);
        if (!test) {
            if (success) emit delivered(status);
            else emit deliveryFailed(status);
        }
    });
    return true;
}
