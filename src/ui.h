#pragma once
#include "core.h"
#include "thruk.h"
#include <QAbstractTableModel>
#include <QSystemTrayIcon>
#include <QTimer>
#include <QWidget>

class QLabel;
class QTableView;
class QSortFilterProxyModel;
class QMediaPlayer;
class QAudioOutput;
class QBuffer;
class QLineEdit;

QColor stateBg(State s);
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
    QString customSound[STATE_COUNT];  // used for WARNING, CRITICAL, DOWN; empty = built-in tone
    bool actions = false;
    QString action[STATE_COUNT];       // shell command per state, UP = the "OK" action
    // last used values of the command dialogs
    bool ackSticky = false, ackNotify = false, ackPersistent = false, ackAllServices = false;
    bool ackExpire = false;  // Nagstamon defaults_acknowledge_expire*
    int ackExpireHours = 2, ackExpireMinutes = 0;
    bool highlightNew = true;  // Nagstamon highlight_new_events
    bool startMaximized = true;
    int closeAction = 0;       // CloseAsk / CloseMinimize / CloseQuit
    QVector<CustomAction> customActions{{"SSH", "ssh $HOST$", true}};
    QString ackComment = "acknowledged", dtComment = "scheduled downtime";
    int dtHours = 2, dtMinutes = 0;
    bool dtFixed = true;
    QPoint barPos{-1, -1};
    QByteArray windowGeometry, headerState;

    static QString path();
    void load();
    void save() const;
};

class StatusModel : public QAbstractTableModel {
    Q_OBJECT
public:
    enum Col { Server, Host, Service, Status, LastCheck, Duration, Attempt, Info, COLS };
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

class StatusBar : public QWidget {
    Q_OBJECT
public:
    StatusBar();
    void setCounts(const int counts[STATE_COUNT], bool error);
    void setFlashing(bool on);
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
    void restyle();
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
    QSortFilterProxyModel *proxy;
    StatusBar *bar;
    QWidget *window;
    QTableView *view;
    QLabel *serverLine;
    QLineEdit *search;
    QLabel *emptyHint;
    QSystemTrayIcon tray;
    QMediaPlayer *player;
    QAudioOutput *audio;
    QBuffer *soundBuf;
    // notification state (Nagstamon statuswindow_properties)
    bool notifying = false;
    State worstNotified = UP;
    QHash<QString, QSet<QString>> previousKeys;  // per server name
    QHash<QString, State> previousWorst;
    QString lastError;
    QHash<QString, bool> eventHistory;  // item key + state -> still fresh (unseen)

    void applyConfig();
    void rebuild(ThrukServer *updated);
    void notifyChange(State diff, State serverWorst, State serverPrevWorst);
    void stopNotifying();
    void playSound(State s);
    void toggleWindow();
    void contextMenu(const QPoint &pos);
    QVector<Item> selectedItems() const;
    ThrukServer *serverOf(const Item &i) const;
    void recheck(const QVector<Item> &items);
    void recheckHostServices(const QVector<Item> &items);
    void runAction(const CustomAction &a, const Item &i);
    void acknowledgeDialog(const QVector<Item> &items);
    void downtimeDialog(const QVector<Item> &items);
    void submitDialog(const Item &item);
    void settingsDialog();
    bool editServer(ServerConf &s);
    void updateTray(State worst, const int counts[STATE_COUNT]);
    void updateEmptyHint();
    bool confirmClose();  // false = keep the window open
    void quit();
    bool quitting = false;
};
