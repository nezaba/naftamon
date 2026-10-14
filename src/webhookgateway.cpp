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
    const bool valid = (nextUrl.scheme() == "http" || nextUrl.scheme() == "https") &&
                       !nextUrl.host().isEmpty() && nextUrl.userInfo().isEmpty();
    const bool nextEnabled = enabled && valid;
    if (enabled_ == nextEnabled && url_ == nextUrl && intervalSeconds_ == nextInterval) return;
    cancelInFlight();
    timer_.stop();
    enabled_ = nextEnabled;
    url_ = nextUrl;
    intervalSeconds_ = nextInterval;
    if (enabled_) {
        timer_.start(intervalSeconds_ * 1000);
        QTimer::singleShot(0, this, &WebhookGateway::sendNow);
    }
}

void WebhookGateway::cancelInFlight() {
    if (!inFlight_) return;
    QNetworkReply *reply = inFlight_;
    inFlight_ = nullptr;
    reply->abort();
}

static QJsonObject alertJson(const Item &item) {
    auto utc = [](qint64 epoch) { return QDateTime::fromSecsSinceEpoch(epoch, Qt::UTC).toString(Qt::ISODate); };
    QJsonObject alert;
    alert["server"] = item.server;
    alert["backend"] = item.backend;
    alert["host"] = item.host;
    alert["service"] = item.service;
    alert["type"] = item.isHost() ? "host" : "service";
    alert["state"] = stateName(item.state);
    alert["hard"] = item.hard;
    alert["acknowledged"] = item.ack;
    alert["downtime"] = item.downtime;
    alert["last_check"] = utc(item.lastCheck);
    alert["last_change"] = utc(item.lastChange);
    return alert;
}

QJsonObject WebhookGateway::snapshot() const {
    QJsonArray statuses, alerts;
    bool complete = !servers_.isEmpty();
    for (const auto &server : servers_) {
        if (!server) { complete = false; continue; }
        const QString status = !server->error.isEmpty() ? "error" : server->hasData ? "ok" : "pending";
        statuses.append(QJsonObject{{"server", server->conf.name}, {"status", status}});
        if (status != "ok") { complete = false; continue; }
        for (const auto *list : {&server->raw.hosts, &server->raw.services})
            for (const Item &item : *list)
                if (item.state != UP) alerts.append(alertJson(item));
    }
    return {{"timestamp", QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs)},
            {"source", "NaftaMon"}, {"complete", complete},
            {"servers", statuses}, {"alerts", alerts}};
}

void WebhookGateway::sendNow() {
    if (!enabled_ || inFlight_) return;
    QNetworkRequest request(url_);
    request.setHeader(QNetworkRequest::ContentTypeHeader, "application/json; charset=utf-8");
    request.setHeader(QNetworkRequest::UserAgentHeader, "NaftaMon WebhookGateway");
    request.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::ManualRedirectPolicy);
    request.setTransferTimeout(timeoutMs_);
    QNetworkReply *reply = network_.post(request, QJsonDocument(snapshot()).toJson(QJsonDocument::Compact));
    inFlight_ = reply;
    QTimer::singleShot(timeoutMs_, reply, [reply] { if (reply->isRunning()) reply->abort(); });
    connect(reply, &QNetworkReply::finished, this, [this, reply] {
        reply->deleteLater();
        if (inFlight_ != reply) return;
        inFlight_ = nullptr;
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        if (reply->error() == QNetworkReply::NoError && status >= 200 && status < 300)
            emit delivered(status);
        else
            emit deliveryFailed(status);
    });
}
