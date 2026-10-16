#include "webhookgateway.h"
#include <QDateTime>
#include <QElapsedTimer>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QMetaObject>
#include <QRunnable>
#include <QThreadPool>
#include <zlib.h>

WebhookGateway::WebhookGateway(QObject *parent, int timeoutMs)
    : QObject(parent), timeoutMs_(qMax(1, timeoutMs)) {
    connect(&timer_, &QTimer::timeout, this, &WebhookGateway::sendNow);
}

void WebhookGateway::setServers(const QVector<ThrukServer *> &servers) {
    QVector<QPointer<ThrukServer>> next;
    next.reserve(servers.size());
    for (ThrukServer *server : servers) next.append(server);
    if (next == servers_) return;
    ++sendGeneration_;
    snapshotPreparing_ = false;
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
            ++sendGeneration_;
            snapshotPreparing_ = false;
            legacyPayloadMode_ = false;
            if (!inFlight_) QTimer::singleShot(0, this, &WebhookGateway::sendNow);
        }
        return;
    }
    ++sendGeneration_;
    snapshotPreparing_ = false;
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

static QJsonObject itemJson(const Item &item, bool includeStatusInformation = true) {
    auto utc = [](qint64 epoch) { return QDateTime::fromSecsSinceEpoch(epoch, Qt::UTC).toString(Qt::ISODate); };
    QJsonObject alert;
    alert["server"] = item.server;
    alert["backend"] = item.backend;
    alert["host"] = item.host;
    alert["service"] = item.service;
    alert["type"] = item.isHost() ? "host" : "service";
    alert["state"] = stateName(item.state);
    if (includeStatusInformation) alert["status_information"] = item.output;
    alert["hard"] = item.hard;
    alert["acknowledged"] = item.ack;
    alert["downtime"] = item.downtime;
    alert["last_check"] = utc(item.lastCheck);
    alert["last_change"] = utc(item.lastChange);
    return alert;
}

static bool gzipCompress(const QByteArray &input, QByteArray *output) {
    z_stream stream{};
    if (deflateInit2(&stream, Z_DEFAULT_COMPRESSION, Z_DEFLATED,
                     MAX_WBITS + 16, 8, Z_DEFAULT_STRATEGY) != Z_OK)
        return false;

    stream.next_in = reinterpret_cast<Bytef *>(const_cast<char *>(input.constData()));
    stream.avail_in = static_cast<uInt>(input.size());
    QByteArray compressed;
    char buffer[64 * 1024];
    int result = Z_OK;
    do {
        stream.next_out = reinterpret_cast<Bytef *>(buffer);
        stream.avail_out = sizeof(buffer);
        result = deflate(&stream, Z_FINISH);
        if (result != Z_OK && result != Z_STREAM_END && result != Z_BUF_ERROR) {
            deflateEnd(&stream);
            return false;
        }
        compressed.append(buffer, sizeof(buffer) - stream.avail_out);
    } while (result != Z_STREAM_END);

    deflateEnd(&stream);
    *output = std::move(compressed);
    return true;
}

QByteArray WebhookGateway::snapshotJson(const QVector<GatewayServerSnapshot> &servers,
                                        const QString &timestamp, bool legacy) {
    QJsonArray serverStatuses;
    bool complete = !servers.isEmpty();
    qsizetype itemCount = 0;
    qsizetype outputCharacters = 0;
    for (const GatewayServerSnapshot &server : servers) {
        serverStatuses.append(QJsonObject{{"server", server.server}, {"status", server.status}});
        if (server.status != "ok") {
            complete = false;
            continue;
        }
        itemCount += server.hosts.size() + server.services.size();
        if (!legacy) itemCount += server.okStatuses.size();
        for (const Item &item : server.hosts) outputCharacters += item.output.size();
        for (const Item &item : server.services) outputCharacters += item.output.size();
        if (!legacy)
            for (const Item &item : server.okStatuses) outputCharacters += item.output.size();
    }

    QJsonObject metadata{{"timestamp", timestamp}, {"source", "NaftaMon"},
                         {"complete", complete}, {"servers", serverStatuses}};
    QByteArray json = QJsonDocument(metadata).toJson(QJsonDocument::Compact);
    if (!json.isEmpty() && json.endsWith('}')) json.chop(1);
    json.reserve(qMax(json.size(), itemCount * 240 + outputCharacters * 2 + 1024));
    json += ",\"alerts\":[";
    bool first = true;
    auto appendAlert = [&](const Item &item) {
        if (item.state == UP) return;
        if (!first) json += ',';
        first = false;
        json += QJsonDocument(itemJson(item, !legacy)).toJson(QJsonDocument::Compact);
    };
    for (const GatewayServerSnapshot &server : servers) {
        if (server.status != "ok") continue;
        for (const Item &item : server.hosts) appendAlert(item);
        for (const Item &item : server.services) appendAlert(item);
    }
    json += ']';
    if (!legacy) {
        json += ",\"ok_statuses\":[";
        bool firstStatus = true;
        for (const GatewayServerSnapshot &server : servers) {
            if (server.status != "ok") continue;
            for (const Item &item : server.okStatuses) {
                if (item.state != UP) continue;
                if (!firstStatus) json += ',';
                firstStatus = false;
                json += QJsonDocument(itemJson(item)).toJson(QJsonDocument::Compact);
            }
        }
        json += ']';
    }
    json += '}';
    return json;
}

void WebhookGateway::sendNow() {
    if (!enabled_ || inFlight_ || snapshotPreparing_) return;
    QElapsedTimer snapshotTimer;
    snapshotTimer.start();
    QVector<GatewayServerSnapshot> input;
    input.reserve(servers_.size());
    for (const auto &server : servers_) {
        if (!server) continue;
        GatewayServerSnapshot copy;
        copy.server = server->conf.name;
        copy.status = !server->error.isEmpty() ? "error" : server->hasData ? "ok" : "pending";
        if (copy.status == "ok") {
            copy.hosts = server->raw.hosts;
            copy.services = server->raw.services;
            copy.okStatuses = server->raw.okStatuses;
        }
        input.append(std::move(copy));
    }
    const qint64 snapshotBuildMs = snapshotTimer.elapsed();
    const QString timestamp = QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs);
    const bool allowFallback = !legacyPayloadMode_;
    const bool legacy = legacyPayloadMode_;
    const QUrl destination = url_;
    const quint64 generation = sendGeneration_;
    snapshotPreparing_ = true;
    QPointer<WebhookGateway> self(this);
    QThreadPool::globalInstance()->start(QRunnable::create(
        [self, input = std::move(input), timestamp, destination, generation, legacy,
         allowFallback, snapshotBuildMs]() mutable {
            QElapsedTimer serializationTimer;
            serializationTimer.start();
            const QByteArray json = WebhookGateway::snapshotJson(input, timestamp, legacy);
            QByteArray legacyJson;
            if (allowFallback) legacyJson = WebhookGateway::snapshotJson(input, timestamp, true);
            const qint64 serializationMs = serializationTimer.elapsed();
            QElapsedTimer compressionTimer;
            compressionTimer.start();
            QByteArray compressed;
            QByteArray legacyCompressed;
            const bool compressedOk = gzipCompress(json, &compressed) &&
                (!allowFallback || gzipCompress(legacyJson, &legacyCompressed));
            const qint64 compressionMs = compressionTimer.elapsed();
            if (!self) return;
            QMetaObject::invokeMethod(self.data(),
                [self, destination, generation, allowFallback, json, compressed,
                 legacyJson, legacyCompressed, compressedOk, snapshotBuildMs,
                 serializationMs, compressionMs]() mutable {
                    if (!self || generation != self->sendGeneration_) return;
                    self->snapshotPreparing_ = false;
                    if (!self->enabled_) return;
                    if (!compressedOk) {
                        emit self->requestFinished(QDateTime::currentDateTime(), false, 0,
                                                   "Could not gzip webhook payload", QString());
                        return;
                    }
                    emit self->snapshotPrepared(json.size(), compressed.size(), snapshotBuildMs,
                                                serializationMs, compressionMs);
                    self->postEncoded(destination, json, compressed, false, allowFallback,
                                      legacyJson, legacyCompressed, true, snapshotBuildMs,
                                      serializationMs, compressionMs);
                }, Qt::QueuedConnection);
        }));
}

bool WebhookGateway::testWebhook(const QString &url) {
    if (!isValidUrl(url) || testInFlight_) return false;
    const QJsonObject payload{{"type", "test"}, {"test", true},
                              {"source", "NaftaMon"},
                              {"timestamp", QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs)},
                              {"message", "NaftaMon test webhook"}};
    return post(QUrl(url.trimmed()), payload, true, false, false);
}

bool WebhookGateway::post(const QUrl &url, const QJsonObject &payload, bool test,
                          bool allowCompatibilityFallback, bool useGzip, qint64 snapshotBuildMs) {
    QElapsedTimer serializationTimer;
    serializationTimer.start();
    const QByteArray json = QJsonDocument(payload).toJson(QJsonDocument::Compact);
    const qint64 serializationMs = serializationTimer.elapsed();
    QByteArray compressed;
    QElapsedTimer gzipTimer;
    gzipTimer.start();
    if (useGzip && !gzipCompress(json, &compressed)) {
        emit requestFinished(QDateTime::currentDateTime(), test, 0,
                             "Could not gzip webhook payload", QString());
        return false;
    }
    const qint64 compressionMs = useGzip ? gzipTimer.elapsed() : 0;
    if (!test) emit snapshotPrepared(json.size(), useGzip ? compressed.size() : json.size(),
                                     snapshotBuildMs, serializationMs, compressionMs);
    return postEncoded(url, json, compressed, test, allowCompatibilityFallback,
                       {}, {}, useGzip, snapshotBuildMs, serializationMs, compressionMs);
}

bool WebhookGateway::postEncoded(const QUrl &url, const QByteArray &json, const QByteArray &gzip,
                                 bool test, bool allowCompatibilityFallback,
                                 const QByteArray &legacyJson, const QByteArray &legacyGzip,
                                 bool useGzip, qint64, qint64, qint64) {
    QPointer<QNetworkReply> &active = test ? testInFlight_ : inFlight_;
    if (active) return false;
    QNetworkRequest request(url);
    request.setHeader(QNetworkRequest::ContentTypeHeader, "application/json; charset=utf-8");
    request.setHeader(QNetworkRequest::UserAgentHeader, "NaftaMon WebhookGateway");
    request.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::ManualRedirectPolicy);
    request.setTransferTimeout(timeoutMs_);
    const QByteArray body = useGzip ? gzip : json;
    if (useGzip) {
        request.setRawHeader("Content-Encoding", "gzip");
    }
    QNetworkReply *reply = network_.post(request, body);
    active = reply;
    QTimer::singleShot(timeoutMs_, reply, [reply] { if (reply->isRunning()) reply->abort(); });
    connect(reply, &QNetworkReply::finished, this,
            [this, reply, test, url, json, gzip, allowCompatibilityFallback,
             legacyJson, legacyGzip, useGzip] {
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
            postEncoded(url, legacyJson, legacyGzip, false, false, {}, {}, true);
            return;
        }
        // A pre-gzip receiver may treat compressed JSON as malformed. Retry once
        // without Content-Encoding; old Web versions with a smaller body limit
        // can still reject very large legacy snapshots with HTTP 413.
        if (!test && useGzip && (status == 400 || status == 415 || status == 501)) {
            postEncoded(url, json, {}, false, allowCompatibilityFallback,
                        legacyJson, legacyGzip, false);
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
