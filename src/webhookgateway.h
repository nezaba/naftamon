#pragma once

#include "thruk.h"
#include <QByteArray>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QDateTime>
#include <QJsonObject>
#include <QPointer>
#include <QTimer>
#include <QUrl>
#include <QVector>

struct GatewayServerSnapshot {
    QString server;
    QString status;
    QVector<Item> hosts;
    QVector<Item> services;
    QVector<Item> okStatuses;
};

class WebhookGateway : public QObject {
    Q_OBJECT
public:
    explicit WebhookGateway(QObject *parent = nullptr, int timeoutMs = 10000);
    void setServers(const QVector<ThrukServer *> &servers);
    void configure(bool enabled, const QString &url, int intervalSeconds);
    bool isEnabled() const { return enabled_; }
    static bool isValidUrl(const QString &url);
    void sendNow();
    bool testWebhook(const QString &url);

signals:
    void delivered(int httpStatus);
    void deliveryFailed(int httpStatus);
    void snapshotPrepared(qint64 jsonBytes, qint64 wireBytes, qint64 snapshotBuildMs,
                          qint64 serializationMs, qint64 gzipMs);
    void requestFinished(const QDateTime &when, bool test, int httpStatus,
                         const QString &error, const QString &response);

private:
    static QByteArray snapshotJson(const QVector<GatewayServerSnapshot> &servers,
                                   const QString &timestamp, bool legacy);
    void cancelInFlight();
    bool post(const QUrl &url, const QJsonObject &payload, bool test,
              bool allowCompatibilityFallback = false, bool useGzip = true,
              qint64 snapshotBuildMs = 0);
    bool postEncoded(const QUrl &url, const QByteArray &json, const QByteArray &gzip,
                     bool test, bool allowCompatibilityFallback,
                     const QByteArray &legacyJson, const QByteArray &legacyGzip,
                     bool useGzip, qint64 snapshotBuildMs = 0,
                     qint64 serializationMs = 0, qint64 gzipMs = 0);

    QVector<QPointer<ThrukServer>> servers_;
    QNetworkAccessManager network_;
    QTimer timer_;
    QPointer<QNetworkReply> inFlight_;
    QPointer<QNetworkReply> testInFlight_;
    QUrl url_;
    bool enabled_ = false;
    bool legacyPayloadMode_ = false;
    bool snapshotPreparing_ = false;
    quint64 sendGeneration_ = 0;
    int intervalSeconds_ = 60;
    int timeoutMs_ = 10000;
};
