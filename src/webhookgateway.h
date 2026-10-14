#pragma once

#include "thruk.h"
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QJsonObject>
#include <QPointer>
#include <QTimer>
#include <QUrl>
#include <QVector>

class WebhookGateway : public QObject {
    Q_OBJECT
public:
    explicit WebhookGateway(QObject *parent = nullptr, int timeoutMs = 10000);
    void setServers(const QVector<ThrukServer *> &servers);
    void configure(bool enabled, const QString &url, int intervalSeconds);
    bool isEnabled() const { return enabled_; }
    void sendNow();

signals:
    void delivered(int httpStatus);
    void deliveryFailed(int httpStatus);

private:
    QJsonObject snapshot() const;
    void cancelInFlight();

    QVector<QPointer<ThrukServer>> servers_;
    QNetworkAccessManager network_;
    QTimer timer_;
    QPointer<QNetworkReply> inFlight_;
    QUrl url_;
    bool enabled_ = false;
    int intervalSeconds_ = 60;
    int timeoutMs_ = 10000;
};
