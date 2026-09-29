// Self-checks: core logic always; Thruk client end-to-end when MOCK_URL is set
// (see tests/run.sh, which starts tests/mock_thruk.py).
#include "../src/core.h"
#include "../src/thruk.h"
#include <QCoreApplication>
#include <QDateTime>
#include <QElapsedTimer>
#include <QEventLoop>
#include <QTimer>
#include <QJsonDocument>
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <cstdio>
#include <cstdlib>

static int failures = 0;
#define CHECK(c) do { if (!(c)) { std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); ++failures; } } while (0)

static Item svc(const QString &host, const QString &name, State st) {
    Item i; i.server = "s"; i.host = host; i.service = i.realService = name; i.state = st;
    i.attempt = 3; i.maxAttempts = 3; return i;
}

static void coreTests() {
    CHECK(humanDuration(0, 5) == "05s");
    CHECK(humanDuration(0, 65) == "01m 05s");
    CHECK(humanDuration(0, 3600 + 61) == "1h 01m 01s");
    CHECK(humanDuration(0, 86400 * 2 + 3600) == "2d 1h 00m 00s");

    RawStatus raw;
    Item down; down.server = "s"; down.host = "h1"; down.state = DOWN; down.ack = true;
    raw.hosts = {down};
    Item soft = svc("h3", "load", WARNING); soft.hard = false;
    Item onDown = svc("h1", "disk", CRITICAL);
    onDown.hostInfo.state = DOWN;  // host flags arrive with the service row (host_* columns)
    onDown.hostInfo.ack = true;
    Item onDowntime = svc("h2", "http", CRITICAL);
    onDowntime.hostInfo.downtime = true;  // UP host in downtime
    raw.services = {onDown, onDowntime, soft};

    Filters f;
    QVector<Item> v = applyFilters(raw, f, 0);
    CHECK(v.size() == 4);
    CHECK(v[1].hostFlags == "A");
    CHECK(worstState(v) == DOWN);

    f.acknowledged = true;
    CHECK(applyFilters(raw, f, 0).size() == 3);
    f = {}; f.servicesOnDowntimeHosts = true;
    CHECK(applyFilters(raw, f, 0).size() == 3);
    f = {}; f.servicesOnDownHosts = true;
    CHECK(applyFilters(raw, f, 0).size() == 3);
    f = {}; f.softServices = true;
    CHECK(applyFilters(raw, f, 0).size() == 3);
    f = {}; f.reHost.enabled = true; f.reHost.re.setPattern("^h1$");
    CHECK(applyFilters(raw, f, 0).size() == 2);
    f.reHost.reverse = true;  // hide everything NOT matching
    CHECK(applyFilters(raw, f, 0).size() == 2);

    // change detection (Nagstamon worst_status_diff)
    QHash<QString, State> st;
    QSet<QString> a = diffKeys({svc("h", "x", WARNING)}, &st);
    QSet<QString> b = diffKeys({svc("h", "x", WARNING), svc("h", "y", CRITICAL)}, &st);
    CHECK(worstNewState(a, b, st) == CRITICAL);
    CHECK(worstNewState(b, a, st) == UP);  // recovery is not worth a notification
    QSet<QString> c = diffKeys({svc("h", "x", CRITICAL)}, &st);
    CHECK(worstNewState(a, c, st) == CRITICAL);  // state change of the same service

    CmdForm form = parseCmdForm("<input type=\"hidden\" name=\"CSRFtoken\" value=\"abc\">"
                                "<input value=\"2026-01-01 10:00:00\" name=\"start_time\">");
    CHECK(form.token == "abc");
    CHECK(form.startTime == "2026-01-01 10:00:00");
    CHECK(parseCmdForm("var CSRFtoken = 'xyz';").token == "xyz");

    QVector<Item> out;
    CHECK(parseStatusJson(R"([{"host_name":"h","description":"d","display_name":"D","state":"2",
        "last_check":100,"last_state_change":50,"plugin_output":"a\nb","current_attempt":1,
        "max_check_attempts":3,"active_checks_enabled":0,"is_flapping":1,"notifications_enabled":1,
        "acknowledged":0,"state_type":0,"scheduled_downtime_depth":1}])", false, "s", true, &out));
    CHECK(out.size() == 1 && out[0].state == CRITICAL && out[0].service == "D" && out[0].realService == "d");
    CHECK(out[0].passive && out[0].flapping && out[0].downtime && !out[0].hard && out[0].output == "a b");
    CHECK(!parseStatusJson("<html>", true, "s", false, &out));
}

static bool waitFor(ThrukServer &s, std::function<bool()> cond, int ms) {
    QElapsedTimer t; t.start();
    while (!cond() && t.elapsed() < ms) {
        QEventLoop l;
        QObject::connect(&s, &ThrukServer::updated, &l, &QEventLoop::quit);
        QTimer::singleShot(50, &l, &QEventLoop::quit);
        l.exec();
    }
    return cond();
}

static const Item *find(const ThrukServer &s, const QString &host, const QString &service) {
    for (const auto *l : {&s.raw.hosts, &s.raw.services})
        for (const Item &i : *l)
            if (i.host == host && i.service == service) return &i;
    return nullptr;
}

static QJsonObject mockStats(const QString &url) {  // url = .../thruk
    QNetworkAccessManager nam;
    QNetworkReply *r = nam.get(QNetworkRequest(QUrl(url + "/_stats")));
    QEventLoop l;
    QObject::connect(r, &QNetworkReply::finished, &l, &QEventLoop::quit);
    l.exec();
    r->deleteLater();
    return QJsonDocument::fromJson(r->readAll()).object();
}

static void thrukTests(const QString &url) {
    ServerConf bad{"m", url, "admin", "wrong", {}};
    ThrukServer b(bad);
    b.refresh();
    CHECK(waitFor(b, [&] { return b.error.startsWith("Login failed"); }, 5000));

    ServerConf u;
    u.url = "http://h/thruk/cgi-bin/";
    CHECK(u.cgiUrl() == "http://h/thruk/cgi-bin");
    u.url = "http://h/thruk";
    CHECK(u.cgiUrl() == "http://h/thruk/cgi-bin");
    ServerConf c{"m", url + "/cgi-bin", "admin", "secret", {}};  // Nagstamon-style CGI URL
    ThrukServer s(c);
    s.refresh();
    CHECK(waitFor(s, [&] { return s.hasData; }, 5000));
    CHECK(s.error.isEmpty());
    CHECK(s.raw.hosts.size() == 1 && s.raw.services.size() == 3);
    const Item *diskp = find(s, "db01", "disk");
    CHECK(diskp && diskp->state == CRITICAL);
    if (!diskp) return;
    Item disk = *diskp;

    QString failed;
    QObject::connect(&s, &ThrukServer::commandFailed, [&](const QString &m) { failed = m; });

    QElapsedTimer t; t.start();
    int fullBefore = mockStats(url).value("full").toInt();
    s.recheck(disk);
    CHECK(s.isRechecking(disk.key()));  // marker shown immediately
    CHECK(waitFor(s, [&] { return !find(s, "db01", "disk"); }, 8000));
    std::printf("recheck -> problem gone in %lld ms (mock check takes 1000 ms)\n", (long long)t.elapsed());
    CHECK(t.elapsed() < 3000);
    CHECK(!s.isRechecking(disk.key()));
    QJsonObject st = mockStats(url);
    int fullPolls = st.value("full").toInt() - fullBefore;  // each full poll = 2 requests (hosts + services)
    std::printf("full status requests during recheck: %d, small follow-up requests so far: %d\n", fullPolls,
                st.value("small").toInt());
    CHECK(fullPolls <= 6);  // was one full poll per second before; now small per-host queries

    t.restart();
    Item web = *find(s, "web01", "");
    s.recheck(web);
    CHECK(waitFor(s, [&] { return !find(s, "web01", ""); }, 8000));
    std::printf("host recheck -> gone in %lld ms\n", (long long)t.elapsed());

    const Item *loadp = find(s, "db01", "load");
    CHECK(loadp);
    if (loadp) {
        Item load = *loadp;  // raw is replaced on every poll
        s.acknowledge(load, "ack", false, false, false);
        CHECK(waitFor(s, [&] { auto *i = find(s, "db01", "load"); return i && i->ack; }, 3000));
        s.downtime(load, "dt", true, "2026-01-01 00:00:00", "2026-01-01 02:00:00", 2, 0);
        CHECK(waitFor(s, [&] { auto *i = find(s, "db01", "load"); return i && i->downtime; }, 3000));
        s.submitResult(load, 0, "fine", "");
        CHECK(waitFor(s, [&] { return !find(s, "db01", "load"); }, 3000));
    }

    // acknowledgement with expiry: sent as use_expire + unix expire_time
    const Item *httpp = find(s, "app01", "http");
    CHECK(httpp && httpp->ack);
    if (httpp) {
        Item http = *httpp;
        qint64 exp = QDateTime::currentSecsSinceEpoch() + 7200;
        s.acknowledge(http, "ack", true, false, false, exp);
        CHECK(waitFor(s, [&] { return mockStats(url)["cmd"].toObject().contains("34"); }, 3000));
        QJsonObject p = mockStats(url)["cmd"].toObject()["34"].toObject();
        CHECK(p["use_expire"].toString() == "on" && p["expire_time"].toString() == QString::number(exp));
        s.removeAcknowledgement(http);
        CHECK(waitFor(s, [&] { auto *i = find(s, "app01", "http"); return i && !i->ack; }, 3000));
    }

    // recheck all services of a host (cmd 17): both follow-up markers, then all gone
    const Item *h2 = find(s, "app01", "http");
    if (h2) {
        Item http = *h2;
        s.recheckHostServices(http, {http});
        CHECK(s.isRechecking(http.key()));
        t.restart();
        CHECK(waitFor(s, [&] { return !find(s, "app01", "http") && !s.isRechecking(http.key()); }, 8000));
        std::printf("recheck all services on host -> gone in %lld ms\n", (long long)t.elapsed());
        CHECK(mockStats(url)["cmd"].toObject()["17"].toObject()["force_check"].toString() == "on");
    }
    CHECK(failed.isEmpty());
    if (!failed.isEmpty()) std::fprintf(stderr, "command failed: %s\n", qPrintable(failed));

    ServerConf down{"m", "http://127.0.0.1:1/thruk", "admin", "secret", {}};
    ThrukServer d(down);
    d.refresh();
    CHECK(waitFor(d, [&] { return !d.error.isEmpty(); }, 5000));
}

int main(int argc, char **argv) {
    QCoreApplication app(argc, argv);
    coreTests();
    QString url = qEnvironmentVariable("MOCK_URL");
    if (!url.isEmpty()) thrukTests(url);
    std::printf(failures ? "%d FAILURE(S)\n" : "all tests passed\n", failures);
    return failures ? 1 : 0;
}
