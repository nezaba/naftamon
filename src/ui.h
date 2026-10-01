#pragma once
#include "core.h"
#include "thruk.h"
#include <QAbstractTableModel>
#include <QSortFilterProxyModel>
#include <QSystemTrayIcon>
#include <QDateTime>
#include <QTimer>
#include <QWidget>

class QLabel;
class QTableView;
class QSoundEffect;
class QLineEdit;
class QToolButton;
class QFrame;

QColor stateBg(State s);  // STATE_COUNT = connection error
QColor stateFg(State s);
QPixmap appIconPixmap(int size);

// Nagstamon-style custom action: shell command with $HOST$, $SERVICE$, $STATUS-INFO$, $USERNAME$, $SERVER$
struct CustomAction {
    QString name, command;
    bool terminal = true;  // run inside a terminal emulator (needed for ssh)
};

enum CloseAction { CloseAsk = 0, CloseMinimize = 1, CloseQuit = 2 };

struct AppConfig {
    QVector<ServerConf> servers;
    int intervalSec = 10;  // Nagstamon default is 60
    bool floatingBar = true;
    Filters filters;
    bool notify = true, flash = true, sound = true, soundRepeat = false, desktop = false;
    bool notifyIf[STATE_COUNT] = {false, true, true, true, true, true};
    QString customSound[STATE_COUNT];  // WAV files for WARNING, CRITICAL, DOWN; empty = built-in tone
    bool actions = false;
    QString action[STATE_COUNT];       // shell command per state, UP = the "OK" action
    // last used values of the command dialogs
    bool ackSticky = false, ackNotify = false, ackPersistent = false, ackAllServices = false;
    bool ackExpire = false;  // Nagstamon defaults_acknowledge_expire*
    int ackExpireHours = 2, ackExpireMinutes = 0;
    bool highlightNew = true;  // Nagstamon highlight_new_events
    bool showAtStart = true;
    bool hideNew = false;  // "N New" quick filter
    bool relativeLastCheck = true;
    int closeAction = 0;       // CloseAsk / CloseMinimize / CloseQuit
    QVector<CustomAction> customActions{{"SSH", "ssh $HOST$", true}};
    QString ackComment = "acknowledged", dtComment = "scheduled downtime";
    int dtHours = 2, dtMinutes = 0;
    bool dtFixed = true;
    QPoint barPos{-1, -1};
    QByteArray windowGeometry, headerState;
    QString notifiedUpdate;  // commit the new-version notice was already shown for
    QColor colorBg[STATE_COUNT + 1], colorFg[STATE_COUNT + 1];  // index STATE_COUNT = connection error

    static QString path();
    void load();
    void loadSettings(const std::function<QVariant(const QString &, const QVariant &)> &get);
    void save() const;
};

class StatusModel : public QAbstractTableModel {
    Q_OBJECT
public:
    enum Col { Backend = BY_BACKEND, Host = BY_HOST, Service = BY_SERVICE, Status = BY_STATUS,
               LastCheck = BY_LAST_CHECK, Duration = BY_DURATION, Attempt = BY_ATTEMPT, Info = BY_INFO, COLS };
    enum { FlagsRole = Qt::UserRole + 1 };  // "ADFPN" flags of Host/Service cells, painted as badges
    bool relativeLastCheck = true;
    bool multiServer = false;  // Backend column also names the server
    QVector<Item> items;
    QSet<QString> rechecking;  // item keys with a recheck in progress
    QSet<QString> fresh;       // item keys of new problems not yet seen (Nagstamon "N" flag)
    void setItems(QVector<Item> v);
    void tick();  // durations changed
    int rowCount(const QModelIndex &) const override { return items.size(); }
    int columnCount(const QModelIndex &) const override { return COLS; }
    QVariant data(const QModelIndex &idx, int role) const override;
    QVariant headerData(int section, Qt::Orientation o, int role) const override;
};

// sorting + search, plus the "New" (hide new problems) toggle
class ItemProxy : public QSortFilterProxyModel {
public:
    const StatusModel *m = nullptr;
    bool hideNew = false;
    State stateOnly = STATE_COUNT;  // status chip filter, STATE_COUNT = all
    QVector<SortKey> sortKeys;      // clicked header columns in click order, first = primary
    void cycleSort(int col);        // not sorted -> ascending -> descending -> not sorted
    QVariant headerData(int section, Qt::Orientation o, int role) const override;
    void refilter() {
#if QT_VERSION >= QT_VERSION_CHECK(6, 9, 0)
        beginFilterChange();
        endFilterChange(Direction::Rows);
#else
        invalidateFilter();  // Qt < 6.9 (e.g. Ubuntu 22.04)
#endif
    }
protected:
    bool filterAcceptsRow(int row, const QModelIndex &parent) const override {
        if (hideNew && m->fresh.contains(m->items[row].key())) return false;
        if (stateOnly < STATE_COUNT && m->items[row].state != stateOnly) return false;
        return QSortFilterProxyModel::filterAcceptsRow(row, parent);
    }
    bool lessThan(const QModelIndex &l, const QModelIndex &r) const override {
        return itemLess(m->items[l.row()], m->items[r.row()], sortKeys);
    }
};

class StatusBar : public QWidget {
    Q_OBJECT
public:
    StatusBar();
    void setCounts(const int counts[STATE_COUNT], bool error);
    void setFlashing(bool on);
    void restyle();  // after a color change
signals:
    void clicked();
protected:
    void mousePressEvent(QMouseEvent *e) override;
    void mouseMoveEvent(QMouseEvent *e) override;
    void mouseReleaseEvent(QMouseEvent *e) override;
private:
    QLabel *labels[STATE_COUNT];
    QLabel *errorLabel;
    QTimer flashTimer;
    bool inverted = false, dragging = false;
    QPoint pressPos;
};

class App : public QObject {
    Q_OBJECT
public:
    App();
    ~App() override;
protected:
    bool eventFilter(QObject *o, QEvent *e) override;

private:
    AppConfig cfg;
    QVector<ThrukServer *> servers;
    QTimer pollTimer, tickTimer;
    StatusModel model;
    ItemProxy *proxy;
    QToolButton *hideAck, *hideDowntime, *hideFlapping, *hideNew;  // quick filters next to search
    QToolButton *updateBtn, *bannerUpdate;  // bannerUpdate: "Update" button of the new-version notice
    QString latestCommit;
    QNetworkAccessManager updateNam;
    StatusBar *bar;
    QWidget *window;
    QTableView *view;
    QLabel *serverLine;
    QLineEdit *search;
    QLabel *emptyHint, *emptyLogo, *bannerText;
    QFrame *banner;
    QTimer bannerTimer;
    QToolButton *chips[STATE_COUNT] = {};
    QSystemTrayIcon tray;
    QSoundEffect *effect = nullptr;  // created on the first sound
    // notification state (Nagstamon statuswindow_properties)
    bool notifying = false;
    State worstNotified = UP;
    QHash<QString, QSet<QString>> previousKeys;  // per server name
    QHash<QString, State> previousWorst;
    QString lastError;
    qint64 seenSince = QDateTime::currentSecsSinceEpoch();  // state changes after this are "new"

    void applyConfig();
    // quiet: re-filter without notifying or marking newly visible rows as new (filter changes)
    void rebuild(ThrukServer *updated, bool quiet = false);
    void syncToggles();
    void notifyChange(State diff, State serverWorst, State serverPrevWorst);
    void stopNotifying();
    void playSound(State s);
    void toggleWindow();
    void contextMenu(const QPoint &pos);
    void backendMenu(const QPoint &at);  // show/hide backends, from the Backend column header
    QVector<Item> selectedItems() const;
    ThrukServer *serverOf(const Item &i) const;
    void recheck(QVector<Item> items);
    bool rebuildQueued = false;
    void recheckHostServices(const QVector<Item> &items);
    void setMaintenance(const QVector<Item> &items, bool enable);  // all checks of their hosts off / on
    void maintenanceDialog();
    void runAction(const CustomAction &a, const Item &i);
    bool openInTerminal(const QString &cmd);
    // quiet: automatic check (start, daily), only speaks up when there is a new version
    void checkForUpdates(bool quiet = false);
    void runUpdate(const QString &latestCommit);
    void acknowledgeDialog(const QVector<Item> &items);
    void downtimeDialog(const QVector<Item> &items);
    void submitDialog(const Item &item);
    void settingsDialog();
    bool editServer(ServerConf &s, const QStringList &taken, const QString &title);
    // reads ~/.nagstamon (read only), asks, merges into `into`; true if anything was imported
    bool importNagstamon(AppConfig &into, bool silentIfNothing, QString dir = {});  // dir: default ~/.nagstamon
    void updateTray(State worst, const int counts[STATE_COUNT]);
    void updateEmptyHint();
    void showBanner(const QString &text, bool error = false);
    void showOnlyState(State st);
    bool lastErrorFree() const;  // no server reports an error
    bool confirmClose();  // false = keep the window open
    void quit();
    bool quitting = false;
};
