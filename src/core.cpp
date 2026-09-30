#include "core.h"
#include <QHash>
#include <algorithm>

static const char *const NAMES[STATE_COUNT] = {"UP", "UNKNOWN", "WARNING", "CRITICAL", "UNREACHABLE", "DOWN"};

QString stateName(State s) { return NAMES[s]; }

QString Item::flags() const {
    QString f;
    if (ack) f += 'A';
    if (downtime) f += 'D';
    if (flapping) f += 'F';
    if (passive) f += 'P';
    return f;
}

QString obfuscate(const QString &plain) {
    QByteArray b = plain.toUtf8();
    for (int i = 0; i < 5; ++i) {
        b = b.toBase64();
        std::reverse(b.begin(), b.end());
        b = qCompress(b);
    }
    return QString::fromLatin1(b.toBase64());
}

QString deobfuscate(const QString &obfuscated) {
    QByteArray b = QByteArray::fromBase64(obfuscated.toLatin1());
    for (int i = 0; i < 5; ++i) {
        if (b.size() < 5) return {};  // not ours; qUncompress would warn
        b = qUncompress(b);
        std::reverse(b.begin(), b.end());
        b = QByteArray::fromBase64(b);
    }
    return QString::fromUtf8(b);
}

QString humanDuration(qint64 since, qint64 now) {
    qint64 td = qMax<qint64>(0, now - since);
    qint64 d = td / 86400, h = td % 86400 / 3600, m = td % 3600 / 60, s = td % 60;
    auto two = [](qint64 v) { return QString::number(v).rightJustified(2, '0'); };
    if (d > 0) return QString("%1d %2h %3m %4s").arg(d).arg(h).arg(two(m), two(s));
    if (h > 0) return QString("%1h %2m %3s").arg(h).arg(two(m), two(s));
    if (m > 0) return QString("%1m %2s").arg(two(m), two(s));
    return two(s) + "s";
}

QVector<Item> applyFilters(const RawStatus &raw, const Filters &f, qint64 now) {
    QVector<Item> out;
    for (const Item &h : raw.hosts) {
        if (h.state == UP) continue;
        bool hide = (h.ack && f.acknowledged) || (h.notifDisabled && f.notificationsDisabled) ||
                    (h.passive && f.checksDisabled) || (h.downtime && f.downtime) ||
                    (h.flapping && f.allFlappingHosts) || (!h.hard && f.softHosts) ||
                    f.reHost.filtersOut(h.host) || f.reInfo.filtersOut(h.output) ||
                    (h.state == DOWN && f.allDownHosts) || (h.state == UNREACHABLE && f.allUnreachableHosts);
        if (!hide) out.append(h);
    }

    for (Item s : raw.services) {
        const HostFlags &hf = s.hostInfo;
        QString attempt = QString("%1/%2").arg(s.attempt).arg(s.maxAttempts);
        bool hide = (s.ack && f.acknowledged) || (s.notifDisabled && f.notificationsDisabled) ||
                    (s.passive && f.checksDisabled) || (s.downtime && f.downtime) ||
                    (s.flapping && f.allFlappingServices) || (hf.downtime && f.servicesOnDowntimeHosts) ||
                    (hf.ack && f.servicesOnAckHosts) || (hf.state == DOWN && f.servicesOnDownHosts) ||
                    (hf.state == UNREACHABLE && f.servicesOnUnreachableHosts) || (!s.hard && f.softServices) ||
                    f.reHost.filtersOut(s.host) || f.reService.filtersOut(s.service) ||
                    f.reInfo.filtersOut(s.output) || f.reDuration.filtersOut(humanDuration(s.lastChange, now)) ||
                    f.reAttempt.filtersOut(attempt) || (s.state == CRITICAL && f.allCriticalServices) ||
                    (s.state == WARNING && f.allWarningServices) || (s.state == UNKNOWN && f.allUnknownServices);
        if (hide) continue;
        if (hf.ack) s.hostFlags += 'A';
        if (hf.downtime) s.hostFlags += 'D';
        if (hf.flapping) s.hostFlags += 'F';
        if (hf.passive) s.hostFlags += 'P';
        out.append(s);
    }
    return out;
}

State worstState(const QVector<Item> &visible) {
    State w = UP;
    for (const Item &i : visible) w = qMax(w, i.state);
    return w;
}

static int compareField(const Item &a, const Item &b, int field) {
    auto num = [](qint64 x, qint64 y) { return x < y ? -1 : x > y ? 1 : 0; };
    auto text = [](const QString &x, const QString &y) { return QString::compare(x, y, Qt::CaseInsensitive); };
    switch (field) {
    case BY_BACKEND:
        if (int c = text(a.server, b.server)) return c;
        return text(a.backend, b.backend);
    case BY_HOST: return text(a.host, b.host);
    case BY_SERVICE: return text(a.service, b.service);
    case BY_STATUS: return num(a.state, b.state);
    case BY_LAST_CHECK: return num(a.lastCheck, b.lastCheck);
    case BY_DURATION: return num(b.lastChange, a.lastChange);  // later change = shorter duration
    case BY_ATTEMPT: return num(a.attempt, b.attempt);
    default: return text(a.output, b.output);
    }
}

bool itemLess(const Item &a, const Item &b, const QVector<SortKey> &keys) {
    for (const SortKey &k : keys)
        if (int c = compareField(a, b, k.field)) return k.descending ? c > 0 : c < 0;
    return false;
}

QSet<QString> diffKeys(const QVector<Item> &visible, QHash<QString, State> *stateOf) {
    QSet<QString> keys;
    for (const Item &i : visible) {
        QString k = i.key() + '\t' + stateName(i.state);
        keys.insert(k);
        if (stateOf) stateOf->insert(k, i.state);
    }
    return keys;
}

State worstNewState(const QSet<QString> &previous, const QSet<QString> &current,
                    const QHash<QString, State> &stateOf) {
    State w = UP;
    for (const QString &k : current)
        if (!previous.contains(k)) w = qMax(w, stateOf.value(k, UP));
    return w;
}
