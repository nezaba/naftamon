#include "ui.h"
#include <QApplication>
#include <QBoxLayout>
#include <QCheckBox>
#include <QClipboard>
#include <QComboBox>
#include <QDataStream>
#include <QDateTime>
#include <QDesktopServices>
#include <QDialog>
#include <QDialogButtonBox>
#include <QFile>
#include <QFileInfo>
#include <QDir>
#include <QFormLayout>
#include <QGridLayout>
#include <QGroupBox>
#include <QHeaderView>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QMenu>
#include <QMessageBox>
#include <QMouseEvent>
#include <QPainter>
#include <QPainterPath>
#include <QProcess>
#include <QProgressDialog>
#include <QStyledItemDelegate>
#include <QPushButton>
#include <QRadioButton>
#include <QScreen>
#include <QSettings>
#include <QSoundEffect>
#include <QShortcut>
#include <QSortFilterProxyModel>
#include <QSpinBox>
#include <QStandardPaths>
#include <QTabWidget>
#include <QTableView>
#include <QTableWidget>
#include <QToolButton>
#include <QJsonDocument>
#include <QJsonObject>
#include <QNetworkReply>
#include <QWindow>
#include <QtMath>

// Nagstamon default colors (config.py)
QColor stateBg(State s) {
    switch (s) {
    case UP: return QColor("#006400");
    case UNKNOWN: return QColor("#FFA500");
    case WARNING: return QColor("#FFFF00");
    case CRITICAL: return QColor("#FF0000");
    case UNREACHABLE: return QColor("#8B0000");
    case DOWN: return QColor("#000000");
    default: return QColor("#D3D3D3");
    }
}
QColor stateFg(State s) {
    return (s == UNKNOWN || s == WARNING) ? QColor("#000000") : QColor("#FFFFFF");
}
static const QColor ERROR_BG("#D3D3D3"), ERROR_FG("#000000");
static const char REPO[] = "nezaba/naftamon";  // GitHub repo used by "Check for updates"
static const State SEVERITY_DESC[] = {DOWN, UNREACHABLE, CRITICAL, UNKNOWN, WARNING};
static const State SOUND_STATES[] = {WARNING, CRITICAL, DOWN};  // Nagstamon STATES_SOUND

static QString lower(State s) { return s == UP ? "ok" : stateName(s).toLower(); }

// ---------------------------------------------------------------- config

QString AppConfig::path() {
    QString env = qEnvironmentVariable("NAFTAMON_CONFIG");
    if (!env.isEmpty()) return env;
    return QStandardPaths::writableLocation(QStandardPaths::AppConfigLocation) + "/naftamon.ini";
}

static QList<QPair<QString, bool *>> boolFields(AppConfig &c) {
    Filters &f = c.filters;
    return {
        {"floating_bar", &c.floatingBar},
        {"filter_all_down_hosts", &f.allDownHosts},
        {"filter_all_unreachable_hosts", &f.allUnreachableHosts},
        {"filter_all_flapping_hosts", &f.allFlappingHosts},
        {"filter_all_unknown_services", &f.allUnknownServices},
        {"filter_all_warning_services", &f.allWarningServices},
        {"filter_all_critical_services", &f.allCriticalServices},
        {"filter_all_flapping_services", &f.allFlappingServices},
        {"filter_acknowledged_hosts_services", &f.acknowledged},
        {"filter_hosts_services_disabled_notifications", &f.notificationsDisabled},
        {"filter_hosts_services_disabled_checks", &f.checksDisabled},
        {"filter_hosts_services_maintenance", &f.downtime},
        {"filter_services_on_acknowledged_hosts", &f.servicesOnAckHosts},
        {"filter_services_on_down_hosts", &f.servicesOnDownHosts},
        {"filter_services_on_hosts_in_maintenance", &f.servicesOnDowntimeHosts},
        {"filter_services_on_unreachable_hosts", &f.servicesOnUnreachableHosts},
        {"filter_hosts_in_soft_state", &f.softHosts},
        {"filter_services_in_soft_state", &f.softServices},
        {"notification", &c.notify},
        {"notification_flashing", &c.flash},
        {"notification_sound", &c.sound},
        {"notification_sound_repeat", &c.soundRepeat},
        {"notification_desktop", &c.desktop},
        {"notification_actions", &c.actions},
        {"ack_sticky", &c.ackSticky},
        {"ack_notify", &c.ackNotify},
        {"ack_persistent", &c.ackPersistent},
        {"ack_all_services", &c.ackAllServices},
        {"downtime_fixed", &c.dtFixed},
        {"ack_expire", &c.ackExpire},
        {"highlight_new_events", &c.highlightNew},
        {"show_window_at_start", &c.showAtStart},
        {"filter_hide_new", &c.hideNew},
        {"last_check_relative", &c.relativeLastCheck},
    };
}

static QList<QPair<QString, RegexFilter *>> regexFields(Filters &f) {
    return {{"re_host", &f.reHost}, {"re_service", &f.reService}, {"re_status_information", &f.reInfo},
            {"re_duration", &f.reDuration}, {"re_attempt", &f.reAttempt}};
}

void AppConfig::load() {
    QSettings s(path(), QSettings::IniFormat);
    for (auto &b : boolFields(*this)) *b.second = s.value(b.first, *b.second).toBool();
    for (auto &r : regexFields(filters)) {
        r.second->enabled = s.value(r.first + "_enabled", false).toBool();
        r.second->reverse = s.value(r.first + "_reverse", false).toBool();
        r.second->re.setPattern(s.value(r.first + "_pattern").toString());
        if (!r.second->re.isValid()) r.second->enabled = false;
    }
    intervalSec = qBound(1, s.value("update_interval_seconds", intervalSec).toInt(), 3600);
    for (int st = 0; st < STATE_COUNT; ++st) {
        notifyIf[st] = s.value("notify_if_" + lower(State(st)), notifyIf[st]).toBool();
        customSound[st] = s.value("notification_custom_sound_" + lower(State(st))).toString();
        action[st] = s.value("notification_action_" + lower(State(st)) + "_string").toString();
    }
    ackComment = s.value("ack_comment", ackComment).toString();
    dtComment = s.value("downtime_comment", dtComment).toString();
    dtHours = s.value("downtime_hours", dtHours).toInt();
    dtMinutes = s.value("downtime_minutes", dtMinutes).toInt();
    barPos = s.value("bar_pos", barPos).toPoint();
    windowGeometry = s.value("window_geometry").toByteArray();
    headerState = s.value("table_header").toByteArray();
    closeAction = qBound(0, s.value("close_action", closeAction).toInt(), 2);
    ackExpireHours = s.value("ack_expire_hours", ackExpireHours).toInt();
    ackExpireMinutes = s.value("ack_expire_minutes", ackExpireMinutes).toInt();
    if (s.contains("custom_actions/size")) {  // absent: keep the default SSH action
        customActions.clear();
        int na = s.beginReadArray("custom_actions");
        for (int i = 0; i < na; ++i) {
            s.setArrayIndex(i);
            customActions.append({s.value("name").toString(), s.value("command").toString(),
                                  s.value("terminal", true).toBool()});
        }
        s.endArray();
    }
    int n = s.beginReadArray("servers");
    for (int i = 0; i < n; ++i) {
        s.setArrayIndex(i);
        ServerConf c;
        c.name = s.value("name").toString();
        c.url = s.value("url").toString();
        c.user = s.value("username").toString();
        c.password = s.value("password").toString();
        c.disabledBackends = s.value("disabled_backends").toString();
        c.enabled = s.value("enabled", true).toBool();
        c.ignoreTls = s.value("ignore_cert", false).toBool();
        c.useDisplayNameService = s.value("use_display_name_service", false).toBool();
        servers.append(c);
    }
    s.endArray();
}

void AppConfig::save() const {
    QString p = path();
    QDir().mkpath(QFileInfo(p).absolutePath());
    {   // create owner-only before any password is written
        QFile f(p);
        if (!f.exists() && f.open(QIODevice::WriteOnly)) f.close();
        f.setPermissions(QFile::ReadOwner | QFile::WriteOwner);
    }
    QSettings s(p, QSettings::IniFormat);
    AppConfig &c = const_cast<AppConfig &>(*this);
    for (auto &b : boolFields(c)) s.setValue(b.first, *b.second);
    for (auto &r : regexFields(c.filters)) {
        s.setValue(r.first + "_enabled", r.second->enabled);
        s.setValue(r.first + "_reverse", r.second->reverse);
        s.setValue(r.first + "_pattern", r.second->re.pattern());
    }
    s.setValue("update_interval_seconds", intervalSec);
    for (int st = 0; st < STATE_COUNT; ++st) {
        s.setValue("notify_if_" + lower(State(st)), notifyIf[st]);
        s.setValue("notification_custom_sound_" + lower(State(st)), customSound[st]);
        s.setValue("notification_action_" + lower(State(st)) + "_string", action[st]);
    }
    s.setValue("ack_comment", ackComment);
    s.setValue("downtime_comment", dtComment);
    s.setValue("downtime_hours", dtHours);
    s.setValue("downtime_minutes", dtMinutes);
    s.setValue("bar_pos", barPos);
    s.setValue("window_geometry", windowGeometry);
    s.setValue("table_header", headerState);
    s.setValue("close_action", closeAction);
    s.setValue("ack_expire_hours", ackExpireHours);
    s.setValue("ack_expire_minutes", ackExpireMinutes);
    s.remove("custom_actions");
    s.beginWriteArray("custom_actions", customActions.size());
    for (int i = 0; i < customActions.size(); ++i) {
        s.setArrayIndex(i);
        s.setValue("name", customActions[i].name);
        s.setValue("command", customActions[i].command);
        s.setValue("terminal", customActions[i].terminal);
    }
    s.endArray();
    s.remove("servers");
    s.beginWriteArray("servers", servers.size());
    for (int i = 0; i < servers.size(); ++i) {
        s.setArrayIndex(i);
        const ServerConf &c = servers[i];
        s.setValue("name", c.name);
        s.setValue("url", c.url);
        s.setValue("username", c.user);
        s.setValue("password", c.password);  // plain text, file is 0600 (Nagstamon without keyring is similar)
        s.setValue("disabled_backends", c.disabledBackends);
        s.setValue("enabled", c.enabled);
        s.setValue("ignore_cert", c.ignoreTls);
        s.setValue("use_display_name_service", c.useDisplayNameService);
    }
    s.endArray();
}

// ---------------------------------------------------------------- model

void StatusModel::setItems(QVector<Item> v) {
    std::sort(v.begin(), v.end(), [](const Item &a, const Item &b) {
        if (a.state != b.state) return a.state > b.state;
        if (a.host != b.host) return a.host < b.host;
        return a.service < b.service;
    });
    beginResetModel();
    items = std::move(v);
    endResetModel();
}

void StatusModel::tick() {
    if (!items.isEmpty()) emit dataChanged(index(0, LastCheck), index(items.size() - 1, Duration), {Qt::DisplayRole});
}

static QString flagText(QChar f) {
    switch (f.unicode()) {
    case 'A': return "Acknowledged";
    case 'D': return "Scheduled downtime";
    case 'F': return "Flapping";
    case 'P': return "Passive only (active checks disabled)";
    case 'N': return "New: state changed since the window was last closed";
    }
    return {};
}

static QString relativeTime(qint64 t, qint64 now) {
    qint64 d = qMax<qint64>(0, now - t);
    if (d < 60) return QString("%1 s ago").arg(d);
    if (d < 3600) return QString("%1 min ago").arg(d / 60);
    if (d < 86400) return QString("%1 h ago").arg(d / 3600);
    return QString("%1 d ago").arg(d / 86400);
}

QVariant StatusModel::data(const QModelIndex &idx, int role) const {
    const Item &i = items[idx.row()];
    if (role == Qt::BackgroundRole) return stateBg(i.state);
    if (role == Qt::ForegroundRole) return stateFg(i.state);
    if (role == FlagsRole) {
        QString n = fresh.contains(i.key()) ? "N" : "";
        if (idx.column() == Host) return i.isHost() ? i.flags() + n : i.hostFlags;
        if (idx.column() == Service && !i.isHost()) return i.flags() + n;
        return {};
    }
    if (role == Qt::ToolTipRole) {
        if (idx.column() == LastCheck) return QDateTime::fromSecsSinceEpoch(i.lastCheck).toString("yyyy-MM-dd HH:mm:ss");
        QStringList tip;
        QString flags = data(idx, FlagsRole).toString();
        for (QChar f : flags) tip << (idx.column() == Host && !i.isHost() ? "Host: " : "") + flagText(f);
        tip << i.output;
        return tip.join('\n');
    }
    if (role == Qt::FontRole) {
        QFont f;
        if (idx.column() == Host) f.setWeight(QFont::DemiBold);
        if (fresh.contains(i.key())) f.setBold(true);
        f.setItalic(rechecking.contains(i.key()));
        return f;
    }
    if (role == Qt::UserRole) {  // sort key
        switch (idx.column()) {
        case Status: return int(i.state);
        case LastCheck: return i.lastCheck;
        case Duration: return -i.lastChange;
        case Attempt: return i.attempt;
        default: return data(idx, Qt::DisplayRole).toString().toLower();
        }
    }
    if (role != Qt::DisplayRole) return {};
    switch (idx.column()) {
    case Server: return i.server;
    case Host: return i.host;
    case Service: return i.service;
    case Status: return stateName(i.state) + (rechecking.contains(i.key()) ? "  ⟳ rechecking…" : "");
    case LastCheck:
        return relativeLastCheck ? relativeTime(i.lastCheck, QDateTime::currentSecsSinceEpoch())
                                 : QDateTime::fromSecsSinceEpoch(i.lastCheck).toString("yyyy-MM-dd HH:mm:ss");
    case Duration: return humanDuration(i.lastChange, QDateTime::currentSecsSinceEpoch());
    case Attempt: return QString("%1/%2").arg(i.attempt).arg(i.maxAttempts);
    case Info: return i.output;
    }
    return {};
}

QVariant StatusModel::headerData(int section, Qt::Orientation o, int role) const {
    static const char *const H[] = {"Server", "Host", "Service", "Status", "Last Check", "Duration", "Attempt",
                                    "Status Information"};
    if (o == Qt::Horizontal && role == Qt::DisplayRole) return H[section];
    return {};
}

// ---------------------------------------------------------------- flag badges, row painting

// Small round badge for a row flag: A check mark, D clock, F zigzag, P pause, N dot.
static void drawBadge(QPainter *p, const QRectF &r, QChar f, const QColor &disk, const QColor &glyph) {
    p->setPen(Qt::NoPen);
    p->setBrush(disk);
    p->drawEllipse(r);
    const double w = r.width(), in = w * 0.28;
    const QRectF g = r.adjusted(in, in, -in, -in);
    const QPointF c = r.center();
    p->setPen(QPen(glyph, qMax(1.2, w * 0.13), Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    p->setBrush(Qt::NoBrush);
    switch (f.unicode()) {
    case 'A': {
        const QPointF pts[] = {{g.left(), c.y()}, {g.left() + g.width() * 0.38, g.bottom()}, {g.right(), g.top()}};
        p->drawPolyline(pts, 3);
        break;
    }
    case 'D':
        p->drawLine(c, QPointF(c.x(), g.top()));
        p->drawLine(c, QPointF(g.right() - g.width() * 0.1, c.y()));
        break;
    case 'F': {
        const QPointF pts[] = {{g.left(), c.y()}, {g.left() + g.width() / 3, g.top()},
                               {g.left() + 2 * g.width() / 3, g.bottom()}, {g.right(), c.y()}};
        p->drawPolyline(pts, 4);
        break;
    }
    case 'P':
        p->drawLine(QPointF(c.x() - w * 0.1, g.top()), QPointF(c.x() - w * 0.1, g.bottom()));
        p->drawLine(QPointF(c.x() + w * 0.1, g.top()), QPointF(c.x() + w * 0.1, g.bottom()));
        break;
    case 'N':
        p->setPen(Qt::NoPen);
        p->setBrush(glyph);
        p->drawEllipse(c, w * 0.2, w * 0.2);
        break;
    }
}

// badge as an icon for the quick-filter buttons, in the palette's text color
static QIcon badgeIcon(QChar f, const QPalette &pal) {
    QPixmap pm(32, 32);
    pm.fill(Qt::transparent);
    QPainter p(&pm);
    p.setRenderHint(QPainter::Antialiasing);
    drawBadge(&p, QRectF(2, 2, 28, 28), f, pal.color(QPalette::Text), pal.color(QPalette::Base));
    return QIcon(pm);
}

// Draws the flags of Host/Service cells as badges after the name, and a faint line under each
// row instead of the full grid. Cost: a few shapes per visible cell.
class RowDelegate : public QStyledItemDelegate {
public:
    using QStyledItemDelegate::QStyledItemDelegate;
    void paint(QPainter *p, const QStyleOptionViewItem &option, const QModelIndex &idx) const override {
        QString flags = idx.data(StatusModel::FlagsRole).toString();
        if (flags.isEmpty()) {
            QStyledItemDelegate::paint(p, option, idx);
        } else {
            QStyleOptionViewItem o(option);
            initStyleOption(&o, idx);
            const QWidget *w = o.widget;
            QStyle *st = w ? w->style() : QApplication::style();
            const int margin = st->pixelMetric(QStyle::PM_FocusFrameHMargin, nullptr, w) + 1;
            const int d = o.fontMetrics.height() - 3, gap = 3, badges = int(flags.size()) * (d + gap) + 4;
            o.text = o.fontMetrics.elidedText(o.text, Qt::ElideRight, qMax(0, o.rect.width() - badges - 2 * margin));
            st->drawControl(QStyle::CE_ItemViewItem, &o, p, w);
            double x = o.rect.left() + margin + o.fontMetrics.horizontalAdvance(o.text) + 6;
            const double y = o.rect.center().y() - d / 2.0 + 0.5;
            const QColor disk = idx.data(Qt::ForegroundRole).value<QColor>();
            const QColor glyph = idx.data(Qt::BackgroundRole).value<QColor>();
            p->save();
            p->setRenderHint(QPainter::Antialiasing);
            for (QChar f : flags) {
                drawBadge(p, QRectF(x, y, d, d), f, disk, glyph);
                x += d + gap;
            }
            p->restore();
        }
        QColor line = option.palette.color(QPalette::Text);
        line.setAlphaF(0.15);
        p->save();
        p->setPen(line);
        p->drawLine(option.rect.bottomLeft(), option.rect.bottomRight());
        p->restore();
    }
};

// ---------------------------------------------------------------- floating status bar

StatusBar::StatusBar() {
    setWindowFlags(Qt::Tool | Qt::FramelessWindowHint | Qt::WindowStaysOnTopHint);
    setWindowTitle("Naftamon");
    auto *l = new QHBoxLayout(this);
    l->setContentsMargins(0, 0, 0, 0);
    l->setSpacing(0);
    for (int s = 0; s < STATE_COUNT; ++s) labels[s] = new QLabel;
    errorLabel = new QLabel(" ERROR ");
    for (State s : SEVERITY_DESC) l->addWidget(labels[s]);
    l->addWidget(labels[UP]);
    l->addWidget(errorLabel);
    labels[UP]->setText(" OK ");
    int none[STATE_COUNT] = {};
    setCounts(none, false);
    flashTimer.setInterval(500);
    connect(&flashTimer, &QTimer::timeout, this, [this] { inverted = !inverted; restyle(); });
    restyle();
}

void StatusBar::setCounts(const int counts[STATE_COUNT], bool error) {
    int total = 0;
    for (State s : SEVERITY_DESC) {
        labels[s]->setText(QString(" %1 %2 ").arg(counts[s]).arg(stateName(s)));
        labels[s]->setVisible(counts[s] > 0);
        total += counts[s];
    }
    labels[UP]->setVisible(total == 0 && !error);
    errorLabel->setVisible(error);
    adjustSize();
}

void StatusBar::setFlashing(bool on) {
    if (on && !flashTimer.isActive()) flashTimer.start();
    if (!on) {
        flashTimer.stop();
        inverted = false;
        restyle();
    }
}

void StatusBar::restyle() {
    auto style = [this](QLabel *l, QColor bg, QColor fg) {
        if (inverted) std::swap(bg, fg);
        l->setStyleSheet(QString("background:%1;color:%2;font-weight:bold;padding:3px 4px;").arg(bg.name(), fg.name()));
    };
    for (int s = 0; s < STATE_COUNT; ++s) style(labels[s], stateBg(State(s)), stateFg(State(s)));
    style(errorLabel, ERROR_BG, ERROR_FG);
}

void StatusBar::mousePressEvent(QMouseEvent *e) {
    pressPos = e->globalPosition().toPoint();
    dragging = false;
}

void StatusBar::mouseMoveEvent(QMouseEvent *e) {
    if ((e->buttons() & Qt::LeftButton) && !dragging &&
        (e->globalPosition().toPoint() - pressPos).manhattanLength() > 4) {
        dragging = true;
        windowHandle()->startSystemMove();  // works on Wayland too
    }
}

void StatusBar::mouseReleaseEvent(QMouseEvent *e) {
    if (e->button() == Qt::LeftButton && !dragging) emit clicked();
    dragging = false;
}

// ---------------------------------------------------------------- app icon

// Big "N" on a red jerrycan ("nafta" = fuel), drawn with QPainter (no SVG module needed).
// Coordinates are 0..100.
QPixmap appIconPixmap(int size) {
    QPixmap pm(size, size);
    pm.fill(Qt::transparent);
    QPainter p(&pm);
    p.setRenderHint(QPainter::Antialiasing);
    p.scale(size / 100.0, size / 100.0);

    QLinearGradient bg(0, 0, 0, 100);
    bg.setColorAt(0, QColor("#2c2f3a"));
    bg.setColorAt(1, QColor("#121318"));
    p.setPen(Qt::NoPen);
    p.setBrush(bg);
    p.drawRoundedRect(QRectF(2, 2, 96, 96), 22, 22);

    const QColor dark("#8e1414");
    // carry handle: top bar with three posts
    p.setBrush(dark);
    p.drawRoundedRect(QRectF(46, 12, 32, 5.5), 2.75, 2.75);
    for (double x : {46.0, 59.75, 73.5}) p.drawRoundedRect(QRectF(x, 12, 4.5, 16), 2.25, 2.25);

    // spout cap on the chamfered corner
    p.save();
    p.translate(27, 24);
    p.rotate(-45);
    p.setBrush(QColor("#5f6b73"));
    p.drawRoundedRect(QRectF(-7, -5, 14, 10), 2.5, 2.5);
    p.setBrush(QColor("#3c464d"));
    p.drawRect(QRectF(-7, 1.5, 14, 2));
    p.restore();

    // can body, top-left corner cut
    QPainterPath body;
    body.moveTo(36, 26);
    body.lineTo(76, 26);
    body.quadTo(84, 26, 84, 34);
    body.lineTo(84, 84);
    body.quadTo(84, 92, 76, 92);
    body.lineTo(24, 92);
    body.quadTo(16, 92, 16, 84);
    body.lineTo(16, 46);
    body.closeSubpath();
    QLinearGradient red(16, 0, 84, 0);
    red.setColorAt(0, QColor("#ef4a3f"));
    red.setColorAt(1, QColor("#c62323"));
    p.setBrush(red);
    p.drawPath(body);
    // pressed-in side panel
    p.setBrush(Qt::NoBrush);
    p.setPen(QPen(QColor(0, 0, 0, 55), 2));
    p.drawRoundedRect(QRectF(24, 36, 52, 48), 5, 5);

    const QPointF n[] = {{33, 78}, {33, 42}, {42, 42}, {58, 66}, {58, 42}, {67, 42},
                         {67, 78}, {58, 78}, {42, 54}, {42, 78}};
    p.setPen(QPen(QColor("#15161b"), 3, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    p.setBrush(QColor("#ffffff"));
    p.drawPolygon(n, 10);
    return pm;
}

// ---------------------------------------------------------------- sound

// Built-in tones (Nagstamon ships .wav files; these are generated instead).
static QByteArray toneWav(State s) {
    const int rate = 22050;
    struct Beep { double hz; int ms; };
    QVector<Beep> beeps = s == DOWN ? QVector<Beep>{{440, 220}, {440, 220}, {440, 220}}
                        : s == CRITICAL ? QVector<Beep>{{1000, 130}, {1000, 130}}
                                        : QVector<Beep>{{880, 160}};
    QByteArray pcm;
    QDataStream ds(&pcm, QIODevice::WriteOnly);
    ds.setByteOrder(QDataStream::LittleEndian);
    for (const Beep &b : beeps) {
        int n = rate * b.ms / 1000, ramp = rate / 200;
        for (int i = 0; i < n; ++i) {
            double env = qMin(1.0, qMin(i, n - i) / double(ramp));
            ds << qint16(0.4 * 32767 * env * qSin(2 * M_PI * b.hz * i / rate));
        }
        for (int i = 0; i < rate * 90 / 1000; ++i) ds << qint16(0);
    }
    QByteArray wav;
    QDataStream h(&wav, QIODevice::WriteOnly);
    h.setByteOrder(QDataStream::LittleEndian);
    h.writeRawData("RIFF", 4);
    h << quint32(36 + pcm.size());
    h.writeRawData("WAVEfmt ", 8);
    h << quint32(16) << quint16(1) << quint16(1) << quint32(rate) << quint32(rate * 2) << quint16(2) << quint16(16);
    h.writeRawData("data", 4);
    h << quint32(pcm.size());
    return wav + pcm;
}

// ---------------------------------------------------------------- app

App::App() : proxy(new ItemProxy) {
    proxy->setParent(this);
    proxy->m = &model;
    cfg.load();
    proxy->setSourceModel(&model);
    proxy->setSortRole(Qt::UserRole);

    window = new QWidget;
    window->setWindowTitle("Naftamon");
    auto *v = new QVBoxLayout(window);
    v->setContentsMargins(6, 6, 6, 4);
    v->setSpacing(4);

    // toolbar: compact search on the left, flat buttons with desktop-theme icons on the right
    auto *top = new QHBoxLayout;
    search = new QLineEdit;
    search->setPlaceholderText("Search host, service, output…");
    search->setToolTip("Filter the list by host, service, status or output (Ctrl+F, Esc clears)");
    search->setClearButtonEnabled(true);
    QIcon findIcon = QIcon::fromTheme("edit-find", QIcon::fromTheme("system-search"));
    if (!findIcon.isNull()) search->addAction(findIcon, QLineEdit::LeadingPosition);
    search->setFixedWidth(search->fontMetrics().horizontalAdvance('x') * 32);
    top->addWidget(search);
    top->addSpacing(8);
    // one chip per problem state with its count; click = show only that state (not saved)
    for (State st : SEVERITY_DESC) {
        auto *c = new QToolButton;
        c->setCheckable(true);
        c->setToolTip("Show only " + stateName(st) + " — click again to show all");
        c->setStyleSheet(QString("QToolButton{background:%1;color:%2;border:2px solid transparent;border-radius:10px;"
                                 "padding:1px 8px;font-weight:600;}"
                                 "QToolButton:checked{border-color:palette(highlight);}")
                             .arg(stateBg(st).name(), stateFg(st).name()));
        c->hide();
        connect(c, &QToolButton::clicked, this, [this, st](bool on) { showOnlyState(on ? st : STATE_COUNT); });
        chips[st] = c;
        top->addWidget(c);
    }
    top->addSpacing(8);
    // quick filters, also the legend of the row flags. All four work alike: pressed = filter on,
    // state is saved. A/D/F are the Settings → Filters switches.
    auto toggle = [&](const QString &text, std::function<void(bool)> set) {
        auto *b = new QToolButton;
        b->setText(text);
        b->setCheckable(true);
        top->addWidget(b);
        connect(b, &QToolButton::clicked, this, [this, set](bool on) {
            set(on);
            cfg.save();
            syncToggles();
            rebuild(nullptr, true);
        });
        return b;
    };
    hideAck = toggle("Acknowledged", [this](bool on) { cfg.filters.acknowledged = on; });
    hideDowntime = toggle("Downtime", [this](bool on) { cfg.filters.downtime = on; });
    hideFlapping = toggle("Flapping", [this](bool on) {
        cfg.filters.allFlappingHosts = cfg.filters.allFlappingServices = on;
    });
    hideNew = toggle("New", [this](bool on) { cfg.hideNew = on; });
    const std::pair<QToolButton *, char> badgeOf[] = {{hideAck, 'A'}, {hideDowntime, 'D'}, {hideFlapping, 'F'}, {hideNew, 'N'}};
    for (auto [b, f] : badgeOf) {
        b->setIcon(badgeIcon(QChar(f), b->palette()));  // same badge as in the rows: doubles as the legend
        b->setToolButtonStyle(Qt::ToolButtonTextBesideIcon);
    }
    top->addStretch();
    auto tool = [&](const char *icon, const QString &text, const QString &tip) {
        auto *b = new QToolButton;
        b->setIcon(QIcon::fromTheme(icon));
        b->setText(text);
        b->setToolTip(tip);
        b->setAutoRaise(true);
        b->setToolButtonStyle(Qt::ToolButtonTextBesideIcon);
        top->addWidget(b);
        return b;
    };
    auto *refreshBtn = tool("view-refresh", "Refresh", "Poll all servers now (F5)");
    auto *recheckAllBtn = tool("system-run", "Recheck all", "Recheck every listed problem");
    auto *settingsBtn = tool("configure", "Settings", "Servers, filters, notifications, actions");
    if (settingsBtn->icon().isNull()) settingsBtn->setIcon(QIcon::fromTheme("preferences-system"));
    v->addLayout(top);

    view = new QTableView;
    view->setModel(proxy);
    view->setSelectionBehavior(QAbstractItemView::SelectRows);
    view->setSelectionMode(QAbstractItemView::ExtendedSelection);
    view->setEditTriggers(QAbstractItemView::NoEditTriggers);
    view->setWordWrap(false);
    view->setContextMenuPolicy(Qt::CustomContextMenu);
    view->setFrameShape(QFrame::NoFrame);
    view->verticalHeader()->hide();
    view->verticalHeader()->setDefaultSectionSize(view->fontMetrics().height() + 11);
    view->setShowGrid(false);  // RowDelegate draws a faint line under each row instead
    view->setItemDelegate(new RowDelegate(view));
    auto *hh = view->horizontalHeader();
    hh->setStretchLastSection(true);
    hh->setDefaultAlignment(Qt::AlignLeft | Qt::AlignVCenter);
    hh->setHighlightSections(false);
    // fixed starting widths from the font (ResizeToContents would measure every row on each refresh)
    const QFontMetrics fm = view->fontMetrics();
    const std::pair<int, const char *> widths[] = {
        {StatusModel::Server, "server-name"}, {StatusModel::Host, "host-name.example.com"},
        {StatusModel::Service, "service description"}, {StatusModel::Status, "UNREACHABLE  ⟳ rech"},
        {StatusModel::LastCheck, cfg.relativeLastCheck ? "59 min ago" : "2026-01-01 00:00:00"}, {StatusModel::Duration, "10d 23h 59m 59s"},
        {StatusModel::Attempt, "Attempt"}};
    for (auto [col, sample] : widths) view->setColumnWidth(col, fm.horizontalAdvance(sample) + 24);
    if (!cfg.headerState.isEmpty()) hh->restoreState(cfg.headerState);
    // header clicks cycle ascending -> descending -> default. Default (at start) is the model's own
    // order: worst state first, then host, service (Nagstamon's default "status descending").
    hh->setSectionsClickable(true);
    hh->setSortIndicatorShown(true);
    hh->setSortIndicator(-1, Qt::AscendingOrder);  // -1 = no column sorted, no arrow
    connect(hh, &QHeaderView::sectionClicked, this, [this, hh](int col) {
        int cur = proxy->sortColumn();
        if (cur != col) proxy->sort(col, Qt::AscendingOrder);
        else if (proxy->sortOrder() == Qt::AscendingOrder) proxy->sort(col, Qt::DescendingOrder);
        else proxy->sort(-1);  // back to the default order
        hh->setSortIndicator(proxy->sortColumn(), proxy->sortOrder());
    });
    proxy->setFilterCaseSensitivity(Qt::CaseInsensitive);
    proxy->setFilterKeyColumn(-1);  // any column
    connect(search, &QLineEdit::textChanged, this, [this](const QString &t) {
        proxy->setFilterFixedString(t);
        updateEmptyHint();
    });
    v->addWidget(view, 1);

    // shown over the empty table: faded logo + text
    emptyLogo = new QLabel(view->viewport());
    QPixmap logo(96, 96);
    logo.fill(Qt::transparent);
    {
        QPainter lp(&logo);
        lp.setOpacity(0.35);
        lp.drawPixmap(0, 0, appIconPixmap(96));
    }
    emptyLogo->setPixmap(logo);
    emptyHint = new QLabel(view->viewport());
    for (QLabel *l : {emptyLogo, emptyHint}) {
        l->setAlignment(Qt::AlignCenter);
        l->setAttribute(Qt::WA_TransparentForMouseEvents);
    }
    auto *vl = new QVBoxLayout(view->viewport());
    vl->addStretch();
    vl->addWidget(emptyLogo);
    vl->addWidget(emptyHint);
    vl->addStretch();

    // inline message banner above the table (instead of pop-ups for non-questions)
    banner = new QFrame;
    auto *bl = new QHBoxLayout(banner);
    bl->setContentsMargins(10, 4, 4, 4);
    bannerText = new QLabel;
    bannerText->setWordWrap(true);
    bannerText->setTextInteractionFlags(Qt::TextSelectableByMouse);
    auto *bannerClose = new QToolButton;
    bannerClose->setText("✕");
    bannerClose->setAutoRaise(true);
    connect(bannerClose, &QToolButton::clicked, banner, &QWidget::hide);
    bl->addWidget(bannerText, 1);
    bl->addWidget(bannerClose);
    banner->hide();
    bannerTimer.setSingleShot(true);
    connect(&bannerTimer, &QTimer::timeout, banner, &QWidget::hide);
    v->insertWidget(v->indexOf(view), banner);

    // per-server state, small and muted at the bottom
    serverLine = new QLabel;
    serverLine->setTextInteractionFlags(Qt::TextSelectableByMouse);
    QFont small = serverLine->font();
    small.setPointSizeF(small.pointSizeF() * 0.9);
    serverLine->setFont(small);
    QPalette sp = serverLine->palette();  // muted, but derived from the theme's text color
    QColor muted = sp.color(QPalette::WindowText);
    muted.setAlphaF(0.7);
    sp.setColor(QPalette::WindowText, muted);
    serverLine->setPalette(sp);
    auto *bottom = new QHBoxLayout;
    bottom->addWidget(serverLine, 1);
    auto *version = new QLabel("v " NAFTAMON_COMMIT);
    version->setFont(small);
    version->setPalette(sp);
    version->setToolTip("Git commit this build was made from");
    bottom->addWidget(version);
    updateBtn = new QToolButton;
    updateBtn->setText("Check for updates");
    updateBtn->setIcon(QIcon::fromTheme("system-software-update"));
    updateBtn->setToolButtonStyle(Qt::ToolButtonTextBesideIcon);
    updateBtn->setAutoRaise(true);
    updateBtn->setFont(small);
    updateBtn->setToolTip(QString("Compare with the latest version on github.com/") + REPO + " and update");
    connect(updateBtn, &QToolButton::clicked, this, &App::checkForUpdates);
    bottom->addWidget(updateBtn);
    v->addLayout(bottom);

    auto *find = new QShortcut(QKeySequence::Find, window);
    connect(find, &QShortcut::activated, search, [this] { search->setFocus(); search->selectAll(); });
    auto *clear = new QShortcut(QKeySequence(Qt::Key_Escape), search);
    clear->setContext(Qt::WidgetShortcut);
    connect(clear, &QShortcut::activated, search, &QLineEdit::clear);
    window->installEventFilter(this);
    // first start: ~60% of the screen, centered; later: the remembered size/position, never maximized
    QRect avail = window->screen()->availableGeometry();
    window->resize(avail.width() * 6 / 10, avail.height() * 6 / 10);
    window->move(avail.center() - window->rect().center());
    if (!cfg.windowGeometry.isEmpty()) window->restoreGeometry(cfg.windowGeometry);
    window->setWindowState(Qt::WindowNoState);

    connect(refreshBtn, &QToolButton::clicked, this, [this] { for (auto *s : servers) s->refresh(); });
    connect(recheckAllBtn, &QToolButton::clicked, this, [this] { recheck(model.items); });
    connect(settingsBtn, &QToolButton::clicked, this, &App::settingsDialog);
    connect(view, &QTableView::customContextMenuRequested, this, &App::contextMenu);
    connect(view, &QTableView::doubleClicked, this, [this] {
        for (const Item &i : selectedItems())
            if (auto *s = serverOf(i)) QDesktopServices::openUrl(s->monitorUrl(i));
    });
    auto shortcut = [this](const char *key, std::function<void()> fn) {
        auto *sc = new QShortcut(QKeySequence(key), view);
        sc->setContext(Qt::WidgetShortcut);
        connect(sc, &QShortcut::activated, this, fn);
    };
    shortcut("R", [this] { recheck(selectedItems()); });
    shortcut("A", [this] { acknowledgeDialog(selectedItems()); });
    shortcut("D", [this] { downtimeDialog(selectedItems()); });
    shortcut("F5", [this] { for (auto *s : servers) s->refresh(); });

    bar = new StatusBar;
    connect(bar, &StatusBar::clicked, this, &App::toggleWindow);

    auto *menu = new QMenu;
    menu->addAction("Show / hide", this, &App::toggleWindow);
    menu->addAction("Refresh", this, [this] { for (auto *s : servers) s->refresh(); });
    menu->addAction("Settings…", this, &App::settingsDialog);
    menu->addSeparator();
    menu->addAction("Quit", this, &App::quit);
    tray.setContextMenu(menu);
    connect(&tray, &QSystemTrayIcon::activated, this, [this](QSystemTrayIcon::ActivationReason r) {
        if (r == QSystemTrayIcon::Trigger) toggleWindow();
    });


    connect(&pollTimer, &QTimer::timeout, this, [this] { for (auto *s : servers) s->refresh(); });
    tickTimer.start(1000);
    connect(&tickTimer, &QTimer::timeout, this, [this] { if (window->isVisible()) model.tick(); });

    applyConfig();
    if (cfg.servers.isEmpty()) QTimer::singleShot(0, this, &App::settingsDialog);

    // development aid: NAFTAMON_SCREENSHOT=file.png renders the status window once and quits
    QString shot = qEnvironmentVariable("NAFTAMON_SCREENSHOT");
    if (!shot.isEmpty())
        QTimer::singleShot(2500, this, [this, shot] {
            window->show();
            QTimer::singleShot(300, this, [this, shot] { window->grab().save(shot); quit(); });
        });
    else if (cfg.showAtStart)
        window->show();
    connect(qApp, &QCoreApplication::aboutToQuit, this, [this] { quitting = true; });
}

void App::quit() {
    quitting = true;
    qApp->quit();
}

// X on the status window: ask (or use the remembered choice) whether to keep running in the tray
bool App::confirmClose() {
    int choice = cfg.closeAction;
    if (choice == CloseAsk) {
        QMessageBox box(QMessageBox::Question, "Naftamon", "Close Naftamon or keep it running?",
                        QMessageBox::NoButton, window);
        box.setInformativeText("Minimized, Naftamon keeps monitoring in the system tray.");
        auto *min = box.addButton("Minimize to tray", QMessageBox::AcceptRole);
        auto *quitBtn = box.addButton("Quit", QMessageBox::DestructiveRole);
        box.addButton(QMessageBox::Cancel);
        box.setDefaultButton(min);
        auto *remember = new QCheckBox("Remember my choice (changeable in Settings → General)");
        box.setCheckBox(remember);
        box.exec();
        if (box.clickedButton() == min) choice = CloseMinimize;
        else if (box.clickedButton() == quitBtn) choice = CloseQuit;
        else return false;  // cancel: keep the window
        if (remember->isChecked()) {
            cfg.closeAction = choice;
            cfg.save();
        }
    }
    if (choice == CloseQuit) {
        QTimer::singleShot(0, this, &App::quit);
        return true;
    }
    // minimize: hide to the tray; without a tray or status bar there is nothing to come back to
    if (!QSystemTrayIcon::isSystemTrayAvailable() && !bar->isVisible()) {
        window->showMinimized();
        return false;
    }
    return true;
}

App::~App() {
    cfg.windowGeometry = window->saveGeometry();
    cfg.headerState = view->horizontalHeader()->saveState();
    if (QGuiApplication::platformName() != "wayland") cfg.barPos = bar->pos();
    cfg.save();
    delete bar;
    delete window;
}

static bool sameConf(const ServerConf &a, const ServerConf &b) {
    return a.name == b.name && a.url == b.url && a.user == b.user && a.password == b.password &&
           a.disabledBackends == b.disabledBackends && a.ignoreTls == b.ignoreTls &&
           a.useDisplayNameService == b.useDisplayNameService;
}

// Keeps connections (session, data) of servers whose settings did not change, so saving
// e.g. a filter change re-filters the data on screen instead of logging in and polling again.
void App::applyConfig() {
    QVector<ThrukServer *> old = servers;
    servers.clear();
    for (const ServerConf &c : cfg.servers) {
        if (!c.enabled) continue;
        auto same = std::find_if(old.begin(), old.end(), [&](ThrukServer *s) { return sameConf(s->conf, c); });
        if (same != old.end()) {
            servers.append(*same);
            old.erase(same);
            continue;
        }
        auto *s = new ThrukServer(c, this);
        connect(s, &ThrukServer::updated, this, [this, s] { rebuild(s); });
        connect(s, &ThrukServer::recheckingChanged, this, [this] { rebuild(nullptr); });
        connect(s, &ThrukServer::commandFailed, this, [this](const QString &msg) {
            lastError = msg;
            showBanner("Command failed: " + msg, true);
            if (!window->isVisible()) tray.showMessage("Naftamon: command failed", msg, QSystemTrayIcon::Warning);
            rebuild(nullptr);
        });
        servers.append(s);
        s->refresh();
    }
    for (auto *s : old) s->deleteLater();  // removed or changed servers
    view->setColumnHidden(StatusModel::Server, servers.size() < 2);
    if (!cfg.flash) bar->setFlashing(false);  // takes effect now, not only for the next notification
    pollTimer.start(cfg.intervalSec * 1000);
    if (cfg.floatingBar) {
        if (cfg.barPos.x() >= 0 && QGuiApplication::platformName() != "wayland") bar->move(cfg.barPos);
        bar->show();
    } else {
        bar->hide();
    }
    tray.show();
    if (!cfg.floatingBar && !QSystemTrayIcon::isSystemTrayAvailable()) window->show();
    model.relativeLastCheck = cfg.relativeLastCheck;
    syncToggles();
    rebuild(nullptr, true);  // filters may have changed: re-filter quietly
}

// pressed = shown; hidden kinds get a struck-through label
// pressed = filter on; hidden kinds also get a struck-through label
void App::syncToggles() {
    const Filters &f = cfg.filters;
    const std::tuple<QToolButton *, bool, const char *> t[] = {
        {hideAck, f.acknowledged, "acknowledged problems"},
        {hideDowntime, f.downtime, "problems in downtime"},
        {hideFlapping, f.allFlappingHosts && f.allFlappingServices, "flapping problems"},
        {hideNew, cfg.hideNew, "new problems (flag N)"}};
    for (auto [b, hidden, what] : t) {
        b->setChecked(hidden);
        QFont font = b->font();
        font.setStrikeOut(hidden);
        b->setFont(font);
        b->setToolTip(QString(hidden ? "Hiding %1 — click to show them" : "Showing %1 — click to hide them").arg(what) +
                      (b == hideNew ? "" : " (same as Settings → Filters)"));
    }
    proxy->hideNew = cfg.hideNew;
    proxy->refilter();
}

ThrukServer *App::serverOf(const Item &i) const {
    for (auto *s : servers)
        if (s->conf.name == i.server) return s;
    return nullptr;
}

void App::rebuild(ThrukServer *updated, bool quiet) {
    qint64 now = QDateTime::currentSecsSinceEpoch();
    QVector<Item> all;
    int counts[STATE_COUNT] = {};
    bool anyError = false;
    QStringList lines;
    for (ThrukServer *s : servers) {
        QVector<Item> vis = s->hasData ? applyFilters(s->raw, cfg.filters, now) : QVector<Item>{};
        if (s == updated && s->error.isEmpty()) {
            QHash<QString, State> st;
            QSet<QString> keys = diffKeys(vis, &st);
            State diff = worstNewState(previousKeys.value(s->conf.name), keys, st);
            previousKeys[s->conf.name] = keys;
            State w = worstState(vis);
            State prevWorst = previousWorst.value(s->conf.name, UP);
            previousWorst[s->conf.name] = w;
            notifyChange(diff, w, prevWorst);
        } else if (quiet && s->hasData && s->error.isEmpty()) {
            previousKeys[s->conf.name] = diffKeys(vis, nullptr);  // new baseline, nothing to notify
            previousWorst[s->conf.name] = worstState(vis);
        }
        anyError |= !s->error.isEmpty();
        lines << (s->error.isEmpty() ? "● " : "✖ ") + s->conf.name + "  " +
                     (s->error.isEmpty() ? (s->hasData ? QString("%1 ms").arg(s->lastRefreshMs) : "connecting…")
                                         : s->error);
        for (const Item &i : vis) counts[i.state]++;
        all += vis;
    }

    // "new" = state changed (Thruk last_state_change) after the status window was last closed,
    // or after startup. Nagstamon instead marks everything unseen by the app, so at start even
    // hours-old problems would be "new".
    model.fresh.clear();
    model.rechecking.clear();
    for (const Item &i : all) {
        if (cfg.highlightNew && i.lastChange > seenSince) model.fresh.insert(i.key());
        if (auto *s = serverOf(i); s && s->isRechecking(i.key())) model.rechecking.insert(i.key());
    }

    // keep selection across the model reset
    QSet<QString> selected;
    for (const Item &i : selectedItems()) selected.insert(i.key());
    model.setItems(all);
    if (!selected.isEmpty()) {
        QItemSelection sel;
        for (int r = 0; r < model.items.size(); ++r)
            if (selected.contains(model.items[r].key())) {
                QModelIndex p = proxy->mapFromSource(model.index(r, 0));
                sel.select(p, p.siblingAtColumn(StatusModel::COLS - 1));
            }
        view->selectionModel()->select(sel, QItemSelectionModel::Select | QItemSelectionModel::Rows);
    }

    for (State st : SEVERITY_DESC) {
        chips[st]->setText(QString("%1 %2").arg(counts[st]).arg(stateName(st)));
        chips[st]->setVisible(counts[st] > 0);
    }
    if (proxy->stateOnly < STATE_COUNT && counts[proxy->stateOnly] == 0) showOnlyState(STATE_COUNT);
    updateEmptyHint();
    bar->setCounts(counts, anyError);
    State worst = worstState(all);
    updateTray(worst, counts);
    QString line = lines.join("     ") + "     updated " + QDateTime::currentDateTime().toString("HH:mm:ss");
    if (!lastError.isEmpty()) line += "     ⚠ last command error: " + lastError;
    serverLine->setText(line);
    if (worst == UP) stopNotifying();
}

// Nagstamon Notification.start(): notify when something new is at least as bad as configured,
// escalate only to a worse state while already notifying.
void App::notifyChange(State diff, State serverWorst, State serverPrevWorst) {
    if (!cfg.notify) return;
    bool soundState = std::find(std::begin(SOUND_STATES), std::end(SOUND_STATES), diff) != std::end(SOUND_STATES);
    if (diff != UP && (diff > worstNotified || !notifying) && cfg.notifyIf[diff]) {
        worstNotified = diff;
        notifying = true;
        if (cfg.flash) bar->setFlashing(true);
        if (cfg.sound && soundState) playSound(diff);
        if (cfg.actions && soundState && !cfg.action[diff].isEmpty())
            QProcess::startDetached("/bin/sh", {"-c", cfg.action[diff]});
        if (cfg.desktop) {
            QStringList parts;
            int counts[STATE_COUNT] = {};
            // model is rebuilt after this call, so count the new data straight from the servers
            qint64 now = QDateTime::currentSecsSinceEpoch();
            for (auto *s : servers)
                if (s->hasData)
                    for (const Item &i : applyFilters(s->raw, cfg.filters, now)) counts[i.state]++;
            for (State s : SEVERITY_DESC)
                if (counts[s]) parts << QString("%1 %2").arg(counts[s]).arg(stateName(s));
            tray.showMessage("Naftamon", parts.join(", "), QSystemTrayIcon::Warning);
        }
    } else if (diff == UP && notifying && cfg.sound && cfg.soundRepeat) {
        playSound(worstNotified);
    }
    // OK action when a server recovers to all-OK
    if (serverWorst == UP && serverPrevWorst != UP && cfg.actions && !cfg.action[UP].isEmpty())
        QProcess::startDetached("/bin/sh", {"-c", cfg.action[UP]});
}

void App::stopNotifying() {
    notifying = false;
    worstNotified = UP;
    bar->setFlashing(false);
}

void App::playSound(State s) {
    if (std::find(std::begin(SOUND_STATES), std::end(SOUND_STATES), s) == std::end(SOUND_STATES)) return;
    // QSoundEffect plays a WAV straight to the audio server. QMediaPlayer (used before) builds a
    // full media pipeline; on Qt 6.2 / GStreamer that loaded OpenBLAS, camera and GPU plugins and was
    // seen using 99% CPU. Created on first use only, so no audio code runs while sound is off.
    QString file = cfg.customSound[s];
    if (file.isEmpty()) {  // built-in tone, written once to the cache dir (QSoundEffect needs a file)
        file = QStandardPaths::writableLocation(QStandardPaths::CacheLocation) + "/tone-" + lower(s) + ".wav";
        QByteArray wav = toneWav(s);
        if (QFileInfo(file).size() != wav.size()) {
            QDir().mkpath(QFileInfo(file).absolutePath());
            QFile f(file);
            if (f.open(QIODevice::WriteOnly)) f.write(wav);
        }
    }
    if (!effect) effect = new QSoundEffect(this);
    if (effect->source() != QUrl::fromLocalFile(file)) effect->setSource(QUrl::fromLocalFile(file));
    effect->play();
}

// Number on the icon: only serious problems (CRITICAL services, DOWN/UNREACHABLE hosts);
// the color still shows the worst state, the tooltip has the full breakdown.
void App::updateTray(State worst, const int counts[STATE_COUNT]) {
    int problems = counts[CRITICAL] + counts[DOWN] + counts[UNREACHABLE];
    bool allError = !servers.isEmpty();
    for (auto *s : servers) allError &= !s->error.isEmpty();
    QPixmap pm(64, 64);
    pm.fill(Qt::transparent);
    QPainter p(&pm);
    p.setRenderHint(QPainter::Antialiasing);
    p.setBrush(allError ? ERROR_BG : stateBg(worst));
    p.setPen(Qt::NoPen);
    p.drawRoundedRect(pm.rect().adjusted(2, 2, -2, -2), 12, 12);
    if (problems > 0) {
        p.setPen(allError ? ERROR_FG : stateFg(worst));
        QFont f = p.font();
        f.setBold(true);
        f.setPixelSize(problems > 99 ? 24 : 34);
        p.setFont(f);
        p.drawText(pm.rect(), Qt::AlignCenter, QString::number(problems));
    }
    p.end();
    tray.setIcon(QIcon(pm));
    QStringList parts;
    for (State s : SEVERITY_DESC)
        if (counts[s]) parts << QString("%1 %2").arg(counts[s]).arg(stateName(s));
    tray.setToolTip("Naftamon: " + (parts.isEmpty() ? QString("all OK") : parts.join(", ")));
}

void App::toggleWindow() {
    if (window->isVisible()) {
        window->hide();
        return;
    }
    stopNotifying();  // Nagstamon stops notifying once the status window is shown
    if (bar->isVisible() && QGuiApplication::platformName() != "wayland")
        window->move(bar->x(), bar->y() + bar->height() + 2);
    model.tick();
    window->show();
    window->raise();
    window->activateWindow();
}

QVector<Item> App::selectedItems() const {
    QVector<Item> out;
    for (const QModelIndex &r : view->selectionModel()->selectedRows())
        out.append(model.items[proxy->mapToSource(r).row()]);
    return out;
}

void App::recheck(const QVector<Item> &items) {
    for (const Item &i : items)
        if (auto *s = serverOf(i)) s->recheck(i);
}

void App::contextMenu(const QPoint &pos) {
    QVector<Item> items = selectedItems();
    if (items.isEmpty()) return;
    QMenu m;
    auto icon = [](const char *name) { return QIcon::fromTheme(name); };
    m.addAction(icon("internet-web-browser"), "Monitor", [=] {
        for (const Item &i : items)
            if (auto *s = serverOf(i)) QDesktopServices::openUrl(s->monitorUrl(i));
    });
    for (const CustomAction &a : cfg.customActions)
        m.addAction(icon("utilities-terminal"), a.name, [=] { for (const Item &i : items) runAction(a, i); });
    m.addSeparator();
    m.addAction(icon("view-refresh"), "Recheck\tR", [=] { recheck(items); });
    m.addAction(icon("view-refresh"), "Recheck all services on host", [=] { recheckHostServices(items); });
    m.addAction(icon("dialog-ok-apply"), "Acknowledge…\tA", [=] { acknowledgeDialog(items); });
    m.addAction(icon("appointment-new"), "Downtime…\tD", [=] { downtimeDialog(items); });
    if (items.size() == 1) m.addAction("Submit check result…", [=] { submitDialog(items[0]); });
    if (std::any_of(items.begin(), items.end(), [](const Item &i) { return i.ack; }))
        m.addAction(icon("edit-undo"), "Remove acknowledgement", [=] {
            for (const Item &i : items)
                if (auto *s = serverOf(i); s && i.ack) s->removeAcknowledgement(i);
        });
    m.addSeparator();
    auto copy = [&](const QString &label, std::function<QString(const Item &)> f) {
        m.addAction(label, [=] {
            QStringList l;
            for (const Item &i : items) l << f(i);
            QApplication::clipboard()->setText(l.join('\n'));
        });
    };
    copy("Copy host", [](const Item &i) { return i.host; });
    copy("Copy service", [](const Item &i) { return i.service; });
    copy("Copy status information", [](const Item &i) { return i.output; });
    m.exec(view->viewport()->mapToGlobal(pos));
}

void App::recheckHostServices(const QVector<Item> &items) {
    QSet<QString> done;  // one command per server + host
    for (const Item &i : items) {
        ThrukServer *s = serverOf(i);
        if (!s || done.contains(i.server + '\t' + i.host)) continue;
        done.insert(i.server + '\t' + i.host);
        QVector<Item> services;  // the host's problem services we know about, for the follow-up
        for (const Item &svc : s->raw.services)
            if (svc.host == i.host) services.append(svc);
        s->recheckHostServices(i, services);
    }
}

static QString shellQuote(QString v) {
    return "'" + v.replace("'", "'\\''") + "'";
}

void App::runAction(const CustomAction &a, const Item &i) {
    ThrukServer *s = serverOf(i);
    QString cmd = a.command;
    // values are quoted: host names or plugin output must never become shell code
    cmd.replace("$HOST$", shellQuote(i.host))
        .replace("$SERVICE$", shellQuote(i.service))
        .replace("$STATUS-INFO$", shellQuote(i.output))
        .replace("$USERNAME$", shellQuote(s ? s->conf.user : QString()))
        .replace("$SERVER$", shellQuote(i.server));
    if (!a.terminal) {
        QProcess::startDetached("/bin/sh", {"-c", cmd});
        return;
    }
    if (!openInTerminal(cmd)) showBanner("No terminal emulator found. Set $TERMINAL or untick \"In terminal\".", true);
}

bool App::openInTerminal(const QString &cmd) {
    // keep the window open when the command fails, so e.g. an ssh error can be read
    // `trap : INT`: Ctrl+C stops the command (handlers reset on exec) but not this wrapper shell,
    // so the terminal does not report "sh crashed" and still shows the message below
    QStringList run{"sh", "-c", "trap : INT; " + cmd + " || { rc=$?; echo; echo \"[exit $rc] press Enter to close\"; read _; }"};
    QString env = qEnvironmentVariable("TERMINAL");
    if (!env.isEmpty() && !QStandardPaths::findExecutable(env).isEmpty())
        return QProcess::startDetached(env, QStringList{"-e"} + run);
    static const QList<QPair<QString, QStringList>> terminals = {
        {"konsole", {"-e"}}, {"gnome-terminal", {"--"}}, {"kgx", {"--"}}, {"xfce4-terminal", {"-x"}},
        {"alacritty", {"-e"}}, {"kitty", {}}, {"foot", {}}, {"xterm", {"-e"}}, {"x-terminal-emulator", {"-e"}}};
    for (const auto &t : terminals)
        if (!QStandardPaths::findExecutable(t.first).isEmpty()) return QProcess::startDetached(t.first, t.second + run);
    return false;
}

// "Check for updates": latest commit of the GitHub repo vs the commit this binary was built from.
// Updating runs in a terminal (progress visible): download, rebuild, reinstall, restart.
void App::checkForUpdates() {
    updateBtn->setEnabled(false);
    updateBtn->setText("Checking…");
    QNetworkRequest req(QUrl(QString("https://api.github.com/repos/%1/commits/main").arg(REPO)));
    req.setRawHeader("Accept", "application/vnd.github+json");
    req.setHeader(QNetworkRequest::UserAgentHeader, "naftamon");
    req.setTransferTimeout(15000);
    QNetworkReply *r = updateNam.get(req);
    connect(r, &QNetworkReply::finished, this, [this, r] {
        r->deleteLater();
        updateBtn->setEnabled(true);
        updateBtn->setText("Check for updates");
        QJsonObject o = QJsonDocument::fromJson(r->readAll()).object();
        QString latest = o["sha"].toString().left(12), mine = NAFTAMON_COMMIT;
        if (r->error() != QNetworkReply::NoError || latest.isEmpty()) {
            showBanner("Could not check for updates: " + r->errorString(), true);
            return;
        }
        if (latest.startsWith(mine.left(12)) && mine != "unknown") {
            showBanner("Naftamon is up to date (" + mine + ").");
            return;
        }
        QString date = o["commit"].toObject()["committer"].toObject()["date"].toString().left(10);
        if (QMessageBox::question(window, "Naftamon",
                                  "A new version of Naftamon is available (" + date + ").\n\nUpdate and restart now?") ==
            QMessageBox::Yes)
            runUpdate(latest);
    });
}

// Download the repo archive, build and install it in the background with a progress dialog;
// output is only shown when something fails. On success the new version is started.
void App::runUpdate(const QString &latest) {
    QString url = QString("https://github.com/%1/archive/refs/heads/main.tar.gz").arg(REPO);
    QString cmd = QString("set -e; d=\"$HOME/.cache/naftamon-update\"; rm -rf \"$d\"; mkdir -p \"$d\"; "
                          "{ curl -fsSL %1 || wget -qO- %1; } | tar xz -C \"$d\"; "
                          "NAFTAMON_COMMIT=%2 sh \"$d\"/naftamon-main/install.sh --update --no-restart")
                      .arg(url, latest);  // latest is hex from GitHub, url is a constant
    auto *proc = new QProcess(this);
    proc->setProcessChannelMode(QProcess::MergedChannels);
    auto *dlg = new QProgressDialog("Downloading…", "Cancel", 0, 0, window);  // 0,0 = busy until the build starts
    dlg->setWindowTitle("Updating Naftamon");
    dlg->setMinimumDuration(0);
    dlg->setAutoClose(false);
    dlg->setAutoReset(false);
    dlg->setMinimumWidth(360);
    auto log = std::make_shared<QByteArray>();
    auto compiled = std::make_shared<int>(0);
    connect(proc, &QProcess::readyRead, dlg, [=] {
        while (proc->canReadLine()) {
            QByteArray line = proc->readLine();
            *log += line;
            if (line.startsWith("@@build")) {
                dlg->setRange(0, 100);
                dlg->setLabelText("Building…");
                dlg->setValue(10);
            } else if (line.startsWith("@@install")) {
                dlg->setLabelText("Installing…");
                dlg->setValue(95);
            } else if (line.contains(" -c ")) {  // one compiler call per source file (~6)
                dlg->setValue(qMin(90, 10 + ++*compiled * 13));
            }
        }
    });
    connect(dlg, &QProgressDialog::canceled, proc, [proc] { proc->kill(); });
    connect(proc, &QProcess::finished, this, [=](int code, QProcess::ExitStatus status) {
        *log += proc->readAll();
        bool canceled = dlg->wasCanceled();
        dlg->deleteLater();
        proc->deleteLater();
        if (canceled) return;
        if (status != QProcess::NormalExit || code != 0) {
            QStringList lines = QString::fromUtf8(*log).trimmed().split('\n');
            QMessageBox box(QMessageBox::Warning, "Naftamon", "The update failed; the current version keeps running.",
                            QMessageBox::Ok, window);
            box.setInformativeText(lines.mid(qMax(0, int(lines.size()) - 6)).join('\n'));
            box.setDetailedText(QString::fromUtf8(*log));
            box.exec();
            return;
        }
        dlg->setValue(100);
        // start the new binary once this process has exited (single-instance lock), then quit
        QProcess::startDetached("/bin/sh", {"-c", "i=0; while pgrep -x naftamon >/dev/null && [ $i -lt 50 ]; do "
                                                  "sleep 0.1; i=$((i + 1)); done; exec \"$HOME/.local/bin/naftamon\""});
        quit();
    });
    proc->start("/bin/sh", {"-c", cmd});
}

void App::updateEmptyHint() {
    bool anyData = std::any_of(servers.begin(), servers.end(), [](ThrukServer *s) { return s->hasData; });
    QString t = proxy->stateOnly < STATE_COUNT && proxy->rowCount() == 0 && !model.items.isEmpty() ? "Nothing to show"
              : !search->text().isEmpty() && proxy->rowCount() == 0 ? "No matches"
              : model.items.isEmpty() && anyData                   ? "✓  All OK — no problems"
              : model.items.isEmpty() && !lastErrorFree()          ? "No connection — see the status line below"
              : model.items.isEmpty()                              ? "Connecting…"
                                                                   : QString();
    emptyHint->setText(t);
    emptyHint->setVisible(!t.isEmpty());
    emptyLogo->setVisible(!t.isEmpty());
}

bool App::lastErrorFree() const {
    return std::none_of(servers.begin(), servers.end(), [](ThrukServer *s) { return !s->error.isEmpty(); });
}

void App::showBanner(const QString &text, bool error) {
    QColor c = error ? QColor("#d9534f") : window->palette().color(QPalette::Highlight);
    QColor bg = c;
    bg.setAlphaF(0.18);
    banner->setStyleSheet(QString("QFrame{background:rgba(%1,%2,%3,%4);border-left:4px solid %5;border-radius:4px;}")
                              .arg(bg.red()).arg(bg.green()).arg(bg.blue()).arg(bg.alphaF()).arg(c.name()));
    bannerText->setText(text);
    banner->show();
    bannerTimer.start(error ? 15000 : 6000);
}

// status chip: show only one state (STATE_COUNT = all)
void App::showOnlyState(State st) {
    for (State s : SEVERITY_DESC) chips[s]->setChecked(s == st);
    proxy->stateOnly = st;
    proxy->refilter();
    updateEmptyHint();
}

bool App::eventFilter(QObject *o, QEvent *e) {
    if (o == window && e->type() == QEvent::Close && !quitting && !confirmClose()) {
        e->ignore();
        return true;
    }
    if (o == window && e->type() == QEvent::Hide) {  // problems have been seen now
        seenSince = QDateTime::currentSecsSinceEpoch();
        rebuild(nullptr);
    }
    return QObject::eventFilter(o, e);
}

static QString targetsText(const QVector<Item> &items) {
    QStringList l;
    for (int n = 0; n < items.size() && n < 8; ++n)
        l << (items[n].isHost() ? items[n].host : items[n].host + " / " + items[n].service);
    if (items.size() > 8) l << QString("… and %1 more").arg(items.size() - 8);
    return l.join('\n');
}

static QDialogButtonBox *okCancel(QDialog *d) {
    auto *b = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel);
    QObject::connect(b, &QDialogButtonBox::accepted, d, &QDialog::accept);
    QObject::connect(b, &QDialogButtonBox::rejected, d, &QDialog::reject);
    return b;
}

void App::acknowledgeDialog(const QVector<Item> &items) {
    if (items.isEmpty()) return;
    QDialog d(window);
    d.setMinimumWidth(520);  // room for wrapped hints and long host names
    d.setWindowTitle("Acknowledge");
    auto *f = new QFormLayout(&d);
    f->addRow("Targets:", new QLabel(targetsText(items)));
    auto *comment = new QLineEdit(cfg.ackComment);
    auto *sticky = new QCheckBox("Sticky acknowledgement");
    auto *notify = new QCheckBox("Send notification");
    auto *persistent = new QCheckBox("Persistent comment");
    auto *allSvc = new QCheckBox("Acknowledge all services on host");
    sticky->setChecked(cfg.ackSticky);
    notify->setChecked(cfg.ackNotify);
    persistent->setChecked(cfg.ackPersistent);
    allSvc->setChecked(cfg.ackAllServices);
    allSvc->setEnabled(std::any_of(items.begin(), items.end(), [](const Item &i) { return i.isHost(); }));
    f->addRow("Comment:", comment);
    f->addRow(sticky);
    f->addRow(notify);
    f->addRow(persistent);
    f->addRow(allSvc);
    auto *expire = new QCheckBox("Expire after");
    auto *expH = new QSpinBox, *expM = new QSpinBox;
    expH->setRange(0, 10000);
    expM->setRange(0, 59);
    expire->setChecked(cfg.ackExpire);
    expH->setValue(cfg.ackExpireHours);
    expM->setValue(cfg.ackExpireMinutes);
    auto *expRow = new QHBoxLayout;
    expRow->addWidget(expire);
    expRow->addWidget(expH);
    expRow->addWidget(new QLabel("h"));
    expRow->addWidget(expM);
    expRow->addWidget(new QLabel("min"));
    f->addRow(expRow);
    f->addRow(okCancel(&d));
    if (d.exec() != QDialog::Accepted) return;
    cfg.ackComment = comment->text();
    cfg.ackSticky = sticky->isChecked();
    cfg.ackNotify = notify->isChecked();
    cfg.ackPersistent = persistent->isChecked();
    cfg.ackAllServices = allSvc->isChecked();
    cfg.ackExpire = expire->isChecked();
    cfg.ackExpireHours = expH->value();
    cfg.ackExpireMinutes = expM->value();
    qint64 expireAt = cfg.ackExpire ? QDateTime::currentSecsSinceEpoch() + cfg.ackExpireHours * 3600 +
                                          cfg.ackExpireMinutes * 60 : 0;
    cfg.save();
    for (const Item &i : items) {
        QVector<Item> also;
        if (i.isHost() && cfg.ackAllServices)  // Nagstamon: all listed services of that host
            for (const Item &s : model.items)
                if (!s.isHost() && s.server == i.server && s.host == i.host) also.append(s);
        if (auto *s = serverOf(i))
            s->acknowledge(i, cfg.ackComment, cfg.ackSticky, cfg.ackNotify, cfg.ackPersistent, expireAt, also);
    }
}

void App::downtimeDialog(const QVector<Item> &items) {
    if (items.isEmpty()) return;
    ThrukServer *srv = serverOf(items[0]);
    if (!srv) return;
    // start/end times come from the server's own form (its timezone), like Nagstamon get_start_end()
    srv->fetchForm(items[0], items[0].isHost() ? 55 : 56, [this, items](CmdForm form) {
        QDialog d(window);
        d.setMinimumWidth(520);
        d.setWindowTitle("Downtime");
        auto *f = new QFormLayout(&d);
        f->addRow("Targets:", new QLabel(targetsText(items)));
        auto *comment = new QLineEdit(cfg.dtComment);
        auto *fixed = new QRadioButton("Fixed");
        auto *flexible = new QRadioButton("Flexible");
        (cfg.dtFixed ? fixed : flexible)->setChecked(true);
        auto *start = new QLineEdit(form.startTime);
        auto *end = new QLineEdit(form.endTime);
        auto *hours = new QSpinBox;
        auto *minutes = new QSpinBox;
        hours->setRange(0, 10000);
        minutes->setRange(0, 59);
        hours->setValue(cfg.dtHours);
        minutes->setValue(cfg.dtMinutes);
        auto *type = new QHBoxLayout;
        type->addWidget(fixed);
        type->addWidget(flexible);
        auto *dur = new QHBoxLayout;
        dur->addWidget(hours);
        dur->addWidget(new QLabel("h"));
        dur->addWidget(minutes);
        dur->addWidget(new QLabel("min"));
        f->addRow("Comment:", comment);
        f->addRow("Type:", type);
        auto *startLabel = new QLabel, *endLabel = new QLabel, *durLabel = new QLabel("Duration:"), *hint = new QLabel;
        hint->setWordWrap(true);
        hint->setForegroundRole(QPalette::PlaceholderText);
        hint->setMinimumHeight(hint->fontMetrics().lineSpacing() * 2 + 4);  // both texts fit, no resize on switch
        f->addRow(startLabel, start);
        f->addRow(endLabel, end);
        f->addRow(durLabel, dur);
        f->addRow(hint);
        f->addRow(okCancel(&d));
        // Nagios semantics: fixed = start..end, duration ignored; flexible = starts when a problem
        // occurs inside the start..end window and then lasts "duration"
        auto sync = [=] {
            bool flex = flexible->isChecked();
            startLabel->setText(flex ? "Window start:" : "Start time:");
            endLabel->setText(flex ? "Window end:" : "End time:");
            for (QWidget *w : {static_cast<QWidget *>(hours), static_cast<QWidget *>(minutes),
                               static_cast<QWidget *>(durLabel)})
                w->setEnabled(flex);
            hint->setText(flex ? "Starts when the host/service has a problem between window start and end, "
                                 "then lasts the duration."
                               : "Active from start to end time. The duration is not used.");
        };
        connect(fixed, &QRadioButton::toggled, &d, sync);
        sync();
        if (d.exec() != QDialog::Accepted) return;
        cfg.dtComment = comment->text();
        cfg.dtFixed = fixed->isChecked();
        cfg.dtHours = hours->value();
        cfg.dtMinutes = minutes->value();
        cfg.save();
        for (const Item &i : items)
            if (auto *s = serverOf(i))
                s->downtime(i, cfg.dtComment, cfg.dtFixed, start->text(), end->text(), cfg.dtHours, cfg.dtMinutes);
    });
}

void App::submitDialog(const Item &item) {
    QDialog d(window);
    d.setMinimumWidth(520);
    d.setWindowTitle("Submit check result");
    auto *f = new QFormLayout(&d);
    f->addRow("Target:", new QLabel(targetsText({item})));
    auto *state = new QComboBox;
    state->addItems(item.isHost() ? QStringList{"UP", "DOWN", "UNREACHABLE"}
                                  : QStringList{"OK", "WARNING", "CRITICAL", "UNKNOWN"});
    auto *output = new QLineEdit;
    auto *perf = new QLineEdit;
    f->addRow("State:", state);
    f->addRow("Check output:", output);
    f->addRow("Performance data:", perf);
    f->addRow(okCancel(&d));
    if (d.exec() != QDialog::Accepted) return;
    if (auto *s = serverOf(item)) s->submitResult(item, state->currentIndex(), output->text(), perf->text());
}

// taken: names of the other servers (a name identifies a server, so it must be unique)
bool App::editServer(ServerConf &s, const QStringList &taken, const QString &title) {
    QDialog d(window);
    d.setMinimumWidth(520);
    d.setWindowTitle(title);
    auto *f = new QFormLayout(&d);
    auto *name = new QLineEdit(s.name);
    auto *url = new QLineEdit(s.url);
    url->setPlaceholderText("http://host/thruk/cgi-bin");
    auto *user = new QLineEdit(s.user);
    auto *pass = new QLineEdit(s.password);
    pass->setEchoMode(QLineEdit::Password);
    auto *backends = new QLineEdit(s.disabledBackends);
    backends->setPlaceholderText("optional, comma separated backend ids");
    auto *enabled = new QCheckBox("Enabled");
    auto *tls = new QCheckBox("Ignore TLS certificate errors");
    auto *disp = new QCheckBox("Use service display name");
    enabled->setChecked(s.enabled);
    tls->setChecked(s.ignoreTls);
    disp->setChecked(s.useDisplayNameService);
    f->addRow("Name:", name);
    f->addRow("Monitor CGI URL:", url);
    f->addRow("Username:", user);
    f->addRow("Password:", pass);
    f->addRow("Disabled backends:", backends);
    f->addRow(enabled);
    f->addRow(tls);
    f->addRow(disp);
    f->addRow(okCancel(&d));
    if (d.exec() != QDialog::Accepted) return false;
    if (name->text().trimmed().isEmpty() || url->text().trimmed().isEmpty()) {
        QMessageBox::warning(window, "Naftamon", "Name and URL are required.");
        return false;
    }
    if (taken.contains(name->text().trimmed())) {
        QMessageBox::warning(window, "Naftamon", "A server named \"" + name->text().trimmed() + "\" already exists.");
        return false;
    }
    s.name = name->text().trimmed();
    s.url = url->text().trimmed();
    s.user = user->text();
    s.password = pass->text();
    s.disabledBackends = backends->text().trimmed();
    s.enabled = enabled->isChecked();
    s.ignoreTls = tls->isChecked();
    s.useDisplayNameService = disp->isChecked();
    return true;
}

void App::settingsDialog() {
    static bool open = false;
    if (open) return;
    open = true;
    AppConfig tmp = cfg;
    QVector<std::function<void()>> commit;
    auto check = [&](const QString &label, bool &ref) {
        auto *c = new QCheckBox(label);
        c->setChecked(ref);
        commit.append([c, &ref] { ref = c->isChecked(); });
        return c;
    };
    auto text = [&](QString &ref, const QString &placeholder = {}) {
        auto *e = new QLineEdit(ref);
        e->setPlaceholderText(placeholder);
        commit.append([e, &ref] { ref = e->text(); });
        return e;
    };

    QDialog d(window);
    d.setWindowTitle("Naftamon settings");
    auto *lay = new QVBoxLayout(&d);
    auto *tabs = new QTabWidget;
    lay->addWidget(tabs);

    // servers
    auto *srvPage = new QWidget;
    auto *srvLay = new QHBoxLayout(srvPage);
    auto *list = new QListWidget;
    auto refill = [&] {
        list->clear();
        for (const ServerConf &s : tmp.servers) list->addItem(s.name + (s.enabled ? "" : " (disabled)") + "  —  " + s.url);
    };
    refill();
    auto *btns = new QVBoxLayout;
    auto *add = new QPushButton("Add…"), *edit = new QPushButton("Edit…"), *copy = new QPushButton("Copy…"),
         *del = new QPushButton("Remove");
    copy->setToolTip("New server with the settings of the selected one (like Nagstamon's \"Copy server\")");
    btns->addWidget(add);
    btns->addWidget(edit);
    btns->addWidget(copy);
    btns->addWidget(del);
    btns->addStretch();
    srvLay->addWidget(list, 1);
    srvLay->addLayout(btns);
    auto namesExcept = [&](int row) {
        QStringList n;
        for (int i = 0; i < tmp.servers.size(); ++i)
            if (i != row) n << tmp.servers[i].name;
        return n;
    };
    connect(add, &QPushButton::clicked, &d, [&] {
        ServerConf s;
        if (editServer(s, namesExcept(-1), "Add server")) { tmp.servers.append(s); refill(); }
    });
    auto editRow = [&] {
        int r = list->currentRow();
        if (r >= 0 && editServer(tmp.servers[r], namesExcept(r), "Edit " + tmp.servers[r].name)) refill();
    };
    connect(copy, &QPushButton::clicked, &d, [&] {
        int r = list->currentRow();
        if (r < 0) return;
        ServerConf s = tmp.servers[r];
        s.name = "Copy of " + s.name;
        if (editServer(s, namesExcept(-1), "Copy " + tmp.servers[r].name)) {
            tmp.servers.append(s);
            refill();
            list->setCurrentRow(tmp.servers.size() - 1);
        }
    });
    connect(edit, &QPushButton::clicked, &d, editRow);
    connect(list, &QListWidget::itemDoubleClicked, &d, editRow);
    connect(del, &QPushButton::clicked, &d, [&] {
        int r = list->currentRow();
        if (r >= 0) { tmp.servers.remove(r); refill(); }
    });
    tabs->addTab(srvPage, "Servers");

    // general
    auto *gen = new QWidget;
    auto *gf = new QFormLayout(gen);
    auto *interval = new QSpinBox;
    interval->setRange(1, 3600);
    interval->setSuffix(" s");
    interval->setValue(tmp.intervalSec);
    commit.append([&, interval] { tmp.intervalSec = interval->value(); });
    gf->addRow("Update interval:", interval);
    gf->addRow(new QLabel("Rechecks and other commands refresh immediately, independent of the interval."));
    gf->addRow(check("Show floating status bar", tmp.floatingBar));
    gf->addRow(check("Highlight new problems (bold, flag N): state changed since the window was last closed",
                     tmp.highlightNew));
    gf->addRow(check("Open the status window at start", tmp.showAtStart));
    auto *lastCheckBox = new QComboBox;
    lastCheckBox->addItems({"Relative (12 s ago)", "Date and time"});
    lastCheckBox->setCurrentIndex(tmp.relativeLastCheck ? 0 : 1);
    commit.append([&tmp, lastCheckBox] { tmp.relativeLastCheck = lastCheckBox->currentIndex() == 0; });
    gf->addRow("Last Check column:", lastCheckBox);
    auto *closeBox = new QComboBox;
    closeBox->addItems({"Ask", "Minimize to tray", "Quit"});
    closeBox->setCurrentIndex(tmp.closeAction);
    commit.append([&tmp, closeBox] { tmp.closeAction = closeBox->currentIndex(); });
    gf->addRow("Closing the window (X):", closeBox);
    gf->addRow(new QLabel("Config file: " + AppConfig::path()));
    tabs->addTab(gen, "General");

    // filters (same options as Nagstamon)
    auto *fil = new QWidget;
    auto *fl = new QVBoxLayout(fil);
    Filters &F = tmp.filters;
    auto *grid = new QGridLayout;
    QList<QPair<QString, bool *>> fchecks = {
        {"Hide all down hosts", &F.allDownHosts},
        {"Hide all unreachable hosts", &F.allUnreachableHosts},
        {"Hide all flapping hosts", &F.allFlappingHosts},
        {"Hide all critical services", &F.allCriticalServices},
        {"Hide all warning services", &F.allWarningServices},
        {"Hide all unknown services", &F.allUnknownServices},
        {"Hide all flapping services", &F.allFlappingServices},
        {"Hide acknowledged hosts and services", &F.acknowledged},
        {"Hide hosts and services with disabled notifications", &F.notificationsDisabled},
        {"Hide hosts and services with disabled checks", &F.checksDisabled},
        {"Hide hosts and services in downtime", &F.downtime},
        {"Hide services on acknowledged hosts", &F.servicesOnAckHosts},
        {"Hide services on down hosts", &F.servicesOnDownHosts},
        {"Hide services on hosts in downtime", &F.servicesOnDowntimeHosts},
        {"Hide services on unreachable hosts", &F.servicesOnUnreachableHosts},
        {"Hide hosts in soft state", &F.softHosts},
        {"Hide services in soft state", &F.softServices},
    };
    for (int i = 0; i < fchecks.size(); ++i) grid->addWidget(check(fchecks[i].first, *fchecks[i].second), i / 2, i % 2);
    fl->addLayout(grid);
    auto *reBox = new QGroupBox("Regular expression filters (matching items are hidden, \"reverse\" hides non-matching)");
    auto *reGrid = new QGridLayout(reBox);
    QStringList reLabels = {"Host", "Service", "Status information", "Duration", "Attempt"};
    auto res = regexFields(F);
    for (int i = 0; i < res.size(); ++i) {
        RegexFilter *r = res[i].second;
        auto *en = new QCheckBox(reLabels[i]);
        auto *pat = new QLineEdit(r->re.pattern());
        auto *rev = new QCheckBox("reverse");
        en->setChecked(r->enabled);
        rev->setChecked(r->reverse);
        reGrid->addWidget(en, i, 0);
        reGrid->addWidget(pat, i, 1);
        reGrid->addWidget(rev, i, 2);
        commit.append([=] {
            r->re.setPattern(pat->text());
            r->reverse = rev->isChecked();
            r->enabled = en->isChecked() && r->re.isValid();
            if (en->isChecked() && !r->re.isValid())
                QMessageBox::warning(window, "Naftamon", "Invalid regular expression, filter disabled: " + pat->text());
        });
    }
    fl->addWidget(reBox);
    fl->addStretch();
    tabs->addTab(fil, "Filters");

    // notifications
    auto *no = new QWidget;
    auto *nf = new QFormLayout(no);
    nf->addRow(check("Enable notifications", tmp.notify));
    nf->addRow(check("Flash status bar", tmp.flash));
    nf->addRow(check("Play sound (warning, critical, down)", tmp.sound));
    nf->addRow(check("Repeat sound on every refresh until the status window is opened", tmp.soundRepeat));
    nf->addRow(check("Desktop notification", tmp.desktop));
    auto *ifs = new QHBoxLayout;
    for (State s : SEVERITY_DESC) ifs->addWidget(check(stateName(s), tmp.notifyIf[s]));
    nf->addRow("Notify on:", ifs);
    for (State s : SOUND_STATES)
        nf->addRow("Custom sound " + stateName(s) + ":", text(tmp.customSound[s], "WAV file, empty = built-in tone"));
    nf->addRow(check("Run notification actions", tmp.actions));
    for (State s : {WARNING, CRITICAL, DOWN, UP})
        nf->addRow("Action " + (s == UP ? QString("OK") : stateName(s)) + ":", text(tmp.action[s], "shell command"));
    tabs->addTab(no, "Notifications");

    // custom actions (Nagstamon "Actions", command type)
    auto *act = new QWidget;
    auto *al = new QVBoxLayout(act);
    al->addWidget(new QLabel("Shown in the right-click menu. Placeholders (shell-quoted): $HOST$ $SERVICE$ "
                             "$STATUS-INFO$ $USERNAME$ $SERVER$.\nTerminal: uses $TERMINAL, else konsole, "
                             "gnome-terminal, kgx, xfce4-terminal, alacritty, kitty, foot, xterm."));
    auto *table = new QTableWidget(0, 3);
    table->setHorizontalHeaderLabels({"Name", "Command", "In terminal"});
    table->horizontalHeader()->setSectionResizeMode(1, QHeaderView::Stretch);
    table->verticalHeader()->hide();
    auto addRow = [table](const CustomAction &a) {
        int r = table->rowCount();
        table->insertRow(r);
        table->setItem(r, 0, new QTableWidgetItem(a.name));
        table->setItem(r, 1, new QTableWidgetItem(a.command));
        auto *t = new QTableWidgetItem;
        t->setFlags(Qt::ItemIsUserCheckable | Qt::ItemIsEnabled);
        t->setCheckState(a.terminal ? Qt::Checked : Qt::Unchecked);
        table->setItem(r, 2, t);
    };
    for (const CustomAction &a : tmp.customActions) addRow(a);
    auto *actBtns = new QHBoxLayout;
    auto *actAdd = new QPushButton("Add"), *actDel = new QPushButton("Remove");
    actBtns->addWidget(actAdd);
    actBtns->addWidget(actDel);
    actBtns->addStretch();
    connect(actAdd, &QPushButton::clicked, &d, [=] { addRow({"New action", "ssh $HOST$", true}); });
    connect(actDel, &QPushButton::clicked, &d, [=] { if (table->currentRow() >= 0) table->removeRow(table->currentRow()); });
    al->addWidget(table);
    al->addLayout(actBtns);
    commit.append([&tmp, table] {
        tmp.customActions.clear();
        for (int r = 0; r < table->rowCount(); ++r) {
            QString name = table->item(r, 0)->text().trimmed(), cmd = table->item(r, 1)->text().trimmed();
            if (!name.isEmpty() && !cmd.isEmpty())
                tmp.customActions.append({name, cmd, table->item(r, 2)->checkState() == Qt::Checked});
        }
    });
    tabs->addTab(act, "Actions");

    lay->addWidget(okCancel(&d));
    d.resize(760, 520);
    bool ok = d.exec() == QDialog::Accepted;
    open = false;
    if (!ok) return;
    for (auto &c : commit) c();
    cfg = tmp;
    cfg.save();
    applyConfig();
}
