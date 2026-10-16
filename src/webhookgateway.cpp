#include "webhookgateway.h"
#include <QDateTime>
#include <QElapsedTimer>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QMetaObject>
#include <QMap>
#include <QRunnable>
#include <QThreadPool>
#include <QUuid>
#include <QDebug>
#include <zlib.h>

WebhookGateway::WebhookGateway(QObject *parent, int timeoutMs)
    : QObject(parent), sourceId_(QUuid::createUuid().toString(QUuid::WithoutBraces)),
      timeoutMs_(qMax(1, timeoutMs)) {
    connect(&timer_, &QTimer::timeout, this, &WebhookGateway::sendNow);
}

void WebhookGateway::setServers(const QVector<ThrukServer *> &servers) {
    QVector<QPointer<ThrukServer>> next;
    next.reserve(servers.size());
    for (ThrukServer *server : servers) next.append(server);
    if (next == servers_) return;
    ++sendGeneration_;
    snapshotPreparing_ = false;
    cancelInFlight(QStringLiteral("server list changed"));
    servers_ = std::move(next);
    if (enabled_) QTimer::singleShot(0, this, &WebhookGateway::sendNow);
}

void WebhookGateway::setSourceId(const QString &sourceId) {
    const QUuid parsed(sourceId);
    if (!parsed.isNull()) sourceId_ = parsed.toString(QUuid::WithoutBraces);
}

void WebhookGateway::setVisibilityContext(const Filters &filters, bool hideNew,
                                         const QSet<QString> &fresh, qint64 nowSeconds) {
    visibilityFilters_ = filters;
    hideNew_ = hideNew;
    freshKeys_ = fresh;
    visibilityTime_ = nowSeconds;
}

void WebhookGateway::configure(bool enabled, const QString &url, int intervalSeconds, int timeoutSeconds) {
    const QUrl nextUrl(url.trimmed());
    const int nextInterval = qBound(1, intervalSeconds, 86400);
    const int nextTimeout = timeoutSeconds < 0 ? timeoutMs_ : qBound(5, timeoutSeconds, 300) * 1000;
    const bool nextEnabled = enabled && isValidUrl(url);
    if (enabled_ == nextEnabled && url_ == nextUrl && intervalSeconds_ == nextInterval && timeoutMs_ == nextTimeout) {
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
    cancelInFlight(nextEnabled ? QStringLiteral("configuration changed") : QStringLiteral("disabled"));
    timer_.stop();
    legacyPayloadMode_ = false;
    enabled_ = nextEnabled;
    url_ = nextUrl;
    intervalSeconds_ = nextInterval;
    timeoutMs_ = nextTimeout;
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

void WebhookGateway::cancelInFlight(const QString &reason) {
    retryPending_ = false;
    if (!inFlight_) return;
    QNetworkReply *reply = inFlight_;
    reply->setProperty("nafta_cancel_reason", reason);
    reply->abort();
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
                                        const QString &timestamp, const QString &sourceId,
                                        const QString &snapshotId, const Filters &filters,
                                        bool hideNew, const QSet<QString> &fresh,
                                        qint64 nowSeconds, bool legacy) {
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
        for (const Item &item : server.hosts) outputCharacters += item.output.size();
        for (const Item &item : server.services) outputCharacters += item.output.size();
    }

    QJsonObject metadata{{"timestamp", timestamp}, {"source", "NaftaMon"},
                         {"complete", complete}, {"servers", serverStatuses}};
    if (!legacy) {
        metadata["schema_version"] = 2;
        metadata["source_id"] = sourceId;
        metadata["snapshot_id"] = snapshotId;
    }
    QByteArray json = QJsonDocument(metadata).toJson(QJsonDocument::Compact);
    if (!json.isEmpty() && json.endsWith('}')) json.chop(1);
    json.reserve(qMax(json.size(), itemCount * 240 + outputCharacters * 2 + 1024));
    json += ",\"alerts\":[";
    bool first = true;
    QMap<QString, int> rawByState, visibleByState;
    int rawTotal = 0, visibleTotal = 0;
    auto appendAlert = [&](const Item &item, const QSet<QString> &visibleKeys) {
        if (item.state == UP) return;
        const bool visible = visibleKeys.contains(item.key());
        if (!first) json += ',';
        first = false;
        QJsonObject object = itemJson(item, !legacy);
        if (!legacy) object["visible_in_desktop"] = visible;
        json += QJsonDocument(object).toJson(QJsonDocument::Compact);
        ++rawTotal;
        ++rawByState[stateName(item.state)];
        if (visible) { ++visibleTotal; ++visibleByState[stateName(item.state)]; }
    };
    for (const GatewayServerSnapshot &server : servers) {
        if (server.status != "ok") continue;
        RawStatus raw;
        raw.hosts = server.hosts;
        raw.services = server.services;
        QSet<QString> visibleKeys;
        for (const Item &item : applyFilters(raw, filters, nowSeconds))
            if (!hideNew || !fresh.contains(item.key())) visibleKeys.insert(item.key());
        for (const Item &item : server.hosts) appendAlert(item, visibleKeys);
        for (const Item &item : server.services) appendAlert(item, visibleKeys);
    }
    json += ']';
    if (!legacy) {
        auto summary = [](int total, const QMap<QString, int> &byState) {
            QJsonObject states;
            for (auto it = byState.cbegin(); it != byState.cend(); ++it) states[it.key()] = it.value();
            return QJsonObject{{"total", total}, {"by_state", states}};
        };
        const QJsonObject counts{{"raw", summary(rawTotal, rawByState)},
                                 {"visible", summary(visibleTotal, visibleByState)}};
        json += ",\"summary\":" + QJsonDocument(counts).toJson(QJsonDocument::Compact) + '}';
    }
    if (legacy) json += '}';
    return json;
}

void WebhookGateway::sendNow() {
    if (!enabled_ || inFlight_ || snapshotPreparing_ || retryPending_) return;
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
        }
        input.append(std::move(copy));
    }
    const qint64 snapshotBuildMs = snapshotTimer.elapsed();
    const QString timestamp = QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs);
    const QString sourceId = sourceId_;
    const QString snapshotId = QUuid::createUuid().toString(QUuid::WithoutBraces);
    const Filters filters = visibilityFilters_;
    const bool hideNew = hideNew_;
    const QSet<QString> fresh = freshKeys_;
    const qint64 nowSeconds = visibilityTime_ > 0 ? visibilityTime_ : QDateTime::currentSecsSinceEpoch();
    const bool allowFallback = !legacyPayloadMode_;
    const bool legacy = legacyPayloadMode_;
    const QUrl destination = url_;
    const quint64 generation = sendGeneration_;
    snapshotPreparing_ = true;
    QPointer<WebhookGateway> self(this);
    QThreadPool::globalInstance()->start(QRunnable::create(
        [self, input = std::move(input), timestamp, sourceId, snapshotId, filters, hideNew,
         fresh, nowSeconds, destination, generation, legacy,
         allowFallback, snapshotBuildMs]() mutable {
            QElapsedTimer serializationTimer;
            serializationTimer.start();
            const QByteArray json = WebhookGateway::snapshotJson(
                input, timestamp, sourceId, snapshotId, filters, hideNew, fresh, nowSeconds, legacy);
            QByteArray legacyJson;
            if (allowFallback) legacyJson = WebhookGateway::snapshotJson(
                input, timestamp, sourceId, snapshotId, filters, hideNew, fresh, nowSeconds, true);
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
                                 bool useGzip, qint64, qint64, qint64,
                                 int retryAttempt, quint64 generation) {
    QPointer<QNetworkReply> &active = test ? testInFlight_ : inFlight_;
    if (active) return false;
    if (!test && generation == 0) generation = sendGeneration_;
    if (!test && retryAttempt == 0) sendDuration_.start();
    QNetworkRequest request(url);
    request.setHeader(QNetworkRequest::ContentTypeHeader, "application/json; charset=utf-8");
    request.setHeader(QNetworkRequest::UserAgentHeader, "NaftaMon WebhookGateway");
    request.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::ManualRedirectPolicy);
    // Use one timeout path so Qt's implicit abort cannot be confused with an
    // explicit cancellation or reported as the generic "Operation canceled".
    request.setTransferTimeout(0);
    const QByteArray body = useGzip ? gzip : json;
    int alertCount = -1;
    const QJsonObject root = QJsonDocument::fromJson(json).object();
    if (root.contains("summary")) alertCount = root.value("summary").toObject().value("raw").toObject().value("total").toInt(-1);
    else alertCount = root.value("alerts").toArray().size();
    if (!test) {
        qInfo().noquote() << QString("Gateway send started attempt=%1/%2 alerts=%3 json_bytes=%4 wire_bytes=%5")
                             .arg(retryAttempt + 1).arg(totalAttempts_).arg(alertCount)
                             .arg(json.size()).arg(body.size());
    }
    if (useGzip) {
        request.setRawHeader("Content-Encoding", "gzip");
    }
    QNetworkReply *reply = network_.post(request, body);
    active = reply;
    QTimer::singleShot(timeoutMs_, reply, [reply, timeoutMs = timeoutMs_] {
        if (reply->isRunning()) {
            reply->setProperty("nafta_timeout_ms", timeoutMs);
            reply->abort();
        }
    });
    connect(reply, &QNetworkReply::finished, this,
            [this, reply, test, url, json, gzip, allowCompatibilityFallback,
             legacyJson, legacyGzip, useGzip, retryAttempt, generation] {
        QPointer<QNetworkReply> &current = test ? testInFlight_ : inFlight_;
        if (current != reply) return;
        current = nullptr;
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        const auto networkError = reply->error();
        const QString cancellation = reply->property("nafta_cancel_reason").toString();
        const int timeoutMs = reply->property("nafta_timeout_ms").toInt();
        bool rejectedExtensions = false;
        if (!test && allowCompatibilityFallback && status == 422) {
            const QByteArray responseBody = reply->readAll();
            rejectedExtensions = responseBody.contains("schema_version") || responseBody.contains("snapshot_id") ||
                                 responseBody.contains("source_id") || responseBody.contains("summary") ||
                                 responseBody.contains("visible_in_desktop") ||
                                 responseBody.contains("status_information") || responseBody.contains("ok_statuses");
        }
        if (rejectedExtensions) {
            // Older NaftaMon Web versions reject unknown fields. Retry the same
            // snapshot without additions and keep that shape for this destination.
            legacyPayloadMode_ = true;
            postEncoded(url, legacyJson, legacyGzip, false, false, {}, {}, true,
                        0, 0, 0, 0, generation);
            reply->deleteLater();
            return;
        }
        // A pre-gzip receiver may treat compressed JSON as malformed. Retry once
        // without Content-Encoding; old Web versions with a smaller body limit
        // can still reject very large legacy snapshots with HTTP 413.
        if (!test && useGzip && (status == 400 || status == 415 || status == 501)) {
            postEncoded(url, json, {}, false, allowCompatibilityFallback,
                        legacyJson, legacyGzip, false, 0, 0, 0, retryAttempt, generation);
            reply->deleteLater();
            return;
        }
        const bool success = networkError == QNetworkReply::NoError && status >= 200 && status < 300;
        const bool retryable = !test && !legacyPayloadMode_ && cancellation.isEmpty() &&
            ((status == 429 || status >= 500) ||
             timeoutMs > 0 ||
             (status == 0 && networkError != QNetworkReply::NoError &&
              networkError != QNetworkReply::OperationCanceledError));
        if (retryable && retryAttempt + 1 < totalAttempts_ && generation == sendGeneration_ && enabled_) {
            retryPending_ = true;
            const int delayMs = 250 * (retryAttempt + 1);
            const QByteArray retryJson = json, retryGzip = gzip, retryLegacyJson = legacyJson, retryLegacyGzip = legacyGzip;
            QTimer::singleShot(delayMs, this, [this, url, retryJson, retryGzip, retryLegacyJson,
                                               retryLegacyGzip, allowCompatibilityFallback, useGzip,
                                               retryAttempt, generation] {
                if (generation != sendGeneration_ || !enabled_) return;
                retryPending_ = false;
                postEncoded(url, retryJson, retryGzip, false, allowCompatibilityFallback,
                            retryLegacyJson, retryLegacyGzip, useGzip, 0, 0, 0,
                            retryAttempt + 1, generation);
            });
            reply->deleteLater();
            return;
        }
        QString error;
        if (!success) {
            if (!cancellation.isEmpty()) error = "Cancelled: " + cancellation;
            else if (timeoutMs > 0) error = QString("Timeout after %1 ms").arg(timeoutMs);
            else if (networkError == QNetworkReply::TimeoutError) error = QString("Timeout after %1 ms").arg(timeoutMs_);
            else if (networkError == QNetworkReply::OperationCanceledError) error = "Cancelled";
            else if (networkError == QNetworkReply::NoError) error = QString("HTTP %1").arg(status);
            else error = reply->errorString();
        }
        QString response;
        if (test && reply->isReadable()) {
            response = QString::fromUtf8(reply->read(4096)).trimmed();
            if (reply->bytesAvailable() > 0) response += "… (truncated)";
        }
        emit requestFinished(QDateTime::currentDateTime(), test, status, error, response);
        if (!test) {
            qInfo().noquote() << QString("Gateway send finished duration_ms=%1 http_status=%2 outcome=%3")
                                 .arg(sendDuration_.isValid() ? sendDuration_.elapsed() : 0)
                                 .arg(status).arg(success ? QStringLiteral("success") :
                                      QString("failure error_code=%1 timeout_ms=%2")
                                          .arg(static_cast<int>(networkError)).arg(timeoutMs));
            if (success) emit delivered(status);
            else emit deliveryFailed(status);
            if (!cancellation.isEmpty() && enabled_ && generation != sendGeneration_)
                QTimer::singleShot(0, this, &WebhookGateway::sendNow);
        }
        reply->deleteLater();
    });
    return true;
}
