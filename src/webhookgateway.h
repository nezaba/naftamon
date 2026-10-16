#pragma once

#include "thruk.h"
#include <QByteArray>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QDateTime>
#include <QElapsedTimer>
#include <QJsonObject>
#include <QPointer>
#include <QTimer>
#include <QUrl>
#include <QVector>
#include <QSet>

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
    explicit WebhookGateway(QObject *parent = nullptr, int timeoutMs = 30000);
    void setServers(const QVector<ThrukServer *> &servers);
    void configure(bool enabled, const QString &url, int intervalSeconds, int timeoutSeconds = -1);
    void setSourceId(const QString &sourceId);
    QString sourceId() const { return sourceId_; }
    void setVisibilityContext(const Filters &filters, bool hideNew, const QSet<QString> &fresh,
                              qint64 nowSeconds);
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
                                   const QString &timestamp, const QString &sourceId,
                                   const QString &snapshotId, const Filters &filters,
                                   bool hideNew, const QSet<QString> &fresh,
                                   qint64 nowSeconds, bool legacy);
    void cancelInFlight(const QString &reason = QStringLiteral("configuration changed"));
    bool post(const QUrl &url, const QJsonObject &payload, bool test,
              bool allowCompatibilityFallback = false, bool useGzip = true,
              qint64 snapshotBuildMs = 0);
    bool postEncoded(const QUrl &url, const QByteArray &json, const QByteArray &gzip,
                     bool test, bool allowCompatibilityFallback,
                     const QByteArray &legacyJson, const QByteArray &legacyGzip,
                     bool useGzip, qint64 snapshotBuildMs = 0,
                     qint64 serializationMs = 0, qint64 gzipMs = 0,
                     int retryAttempt = 0, quint64 generation = 0);

    QVector<QPointer<ThrukServer>> servers_;
    QNetworkAccessManager network_;
    QTimer timer_;
    QPointer<QNetworkReply> inFlight_;
    QPointer<QNetworkReply> testInFlight_;
    QUrl url_;
    QString sourceId_;
    Filters visibilityFilters_;
    QSet<QString> freshKeys_;
    qint64 visibilityTime_ = 0;
    bool hideNew_ = false;
    bool enabled_ = false;
    bool legacyPayloadMode_ = false;
    bool snapshotPreparing_ = false;
    bool retryPending_ = false;
    quint64 sendGeneration_ = 0;
    int intervalSeconds_ = 60;
    int timeoutMs_ = 30000;
    int totalAttempts_ = 3;
    QElapsedTimer sendDuration_;
};
