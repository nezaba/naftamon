// Monitoring data model, filtering and change detection. No GUI or network code here.
#pragma once
#include <QString>
#include <QStringList>
#include <QVector>
#include <QSet>
#include <QHash>
#include <QRegularExpression>

// Severity order is Nagstamon's STATES list restricted to the states Nagios/Thruk can report.
enum State { UP = 0, UNKNOWN, WARNING, CRITICAL, UNREACHABLE, DOWN, STATE_COUNT };
QString stateName(State s);

// state and flags of a service's host (from the service row: host_state, host_acknowledged, ...)
struct HostFlags { bool ack = false, downtime = false, flapping = false, passive = false; State state = UP; };

struct Item {
    QString server, host, service;  // service empty => host item
    QString realService;            // service description sent in commands (differs if display name used)
    State state = UP;
    bool hard = true;
    qint64 lastCheck = 0, lastChange = 0;
    int attempt = 0, maxAttempts = 0;
    QString output;
    bool ack = false, downtime = false, flapping = false, passive = false, notifDisabled = false;
    HostFlags hostInfo;  // services only
    // flags of the service's host (Nagstamon's host_flags column), empty for host items
    QString hostFlags;

    bool isHost() const { return service.isEmpty(); }
    QString flags() const;  // "A","D","F","P" like Nagstamon
    QString key() const { return server + '\t' + host + '\t' + service; }
};

// Nagstamon's human_readable_duration_from_timestamp()
QString humanDuration(qint64 since, qint64 now);

struct RegexFilter {
    bool enabled = false, reverse = false;
    QRegularExpression re;
    // Nagstamon is_found_by_re(): true => item is filtered out
    bool filtersOut(const QString &s) const {
        if (!enabled) return false;
        return re.match(s).hasMatch() != reverse;
    }
};

// Same names/semantics as Nagstamon's conf.filter_* options.
struct Filters {
    bool allDownHosts = false, allUnreachableHosts = false, allFlappingHosts = false;
    bool allUnknownServices = false, allWarningServices = false, allCriticalServices = false,
         allFlappingServices = false;
    bool acknowledged = false, notificationsDisabled = false, checksDisabled = false, downtime = false;
    bool servicesOnAckHosts = false, servicesOnDownHosts = false, servicesOnDowntimeHosts = false,
         servicesOnUnreachableHosts = false;
    bool softHosts = false, softServices = false;
    RegexFilter reHost, reService, reInfo, reDuration, reAttempt;
};

// Result of one poll of one server: DOWN/UNREACHABLE host rows and service problem rows.
struct RawStatus {
    QVector<Item> hosts;
    QVector<Item> services;
};

// Nagstamon GenericServer.get_status() filter pass: returns visible problem items.
QVector<Item> applyFilters(const RawStatus &raw, const Filters &f, qint64 now);

State worstState(const QVector<Item> &visible);

// Nagstamon worst_status_diff: worst state among items that are new (by host/service/state)
// compared to the previous poll; UP if nothing new.
State worstNewState(const QSet<QString> &previous, const QSet<QString> &current,
                    const QHash<QString, State> &stateOf);
QSet<QString> diffKeys(const QVector<Item> &visible, QHash<QString, State> *stateOf);
