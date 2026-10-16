// Self-checks: core logic always; Thruk client end-to-end when MOCK_URL is set
// (see tests/run.sh, which starts tests/mock_thruk.py).
#include "../src/core.h"
#include "../src/thruk.h"
#include "../src/webhookgateway.h"
#include "../src/ui.h"
#include <QApplication>
#include <QDateTime>
#include <QElapsedTimer>
#include <QEventLoop>
#include <QTimer>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QTemporaryDir>
#include <QSettings>
#include <QDialog>
#include <QDialogButtonBox>
#include <QLabel>
#include <QLineEdit>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QSpinBox>
#include <QTableView>
#include <QToolButton>
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

    for (const QString &pw : {QString(), QString("secret"), QString("pä ss\\w0rd=\"x\"")})
        CHECK(deobfuscate(obfuscate(pw)) == pw);
    CHECK(!obfuscate("secret").contains("secret") && obfuscate("secret") != obfuscate("secres"));
    CHECK(deobfuscate("not ours").isEmpty() && deobfuscate("").isEmpty());
    // made by Nagstamon's Config.obfuscate() (Python)
    CHECK(nagstamonDeobfuscate("eJwFwUEOgjAQAMAHeQCJMe3BQ11XCtsVCRAfQEwTTKzSBtN9vTMnZeuHWHMNqL+Zosue1wDwOU7cYjHuDz39Cg/l5Obw8gNJo2MFLhoBPl9SB8utwzcn2LhZSe47lBiQKtWPbCwk5fVWDkly+/wDe2Ajgg==") == "admin");
    CHECK(nagstamonDeobfuscate("eJwFwdEKwiAUANBP6lor9NEpQTlNYxE+rpG0oFmYOe/Xdw7AY79I16rz7/Oe6vNURFJwoQU9W5CLOvRss6WKhBVmb2kS1kR/o2siMJVJpddAVXT64GXN13nUuEvRB2G5YWS0RLXBQNdpyICZul7zmemmkQncF+vx/gfTYCqB") == "pä ss\\w0rd");
    CHECK(nagstamonDeobfuscate("plain old password").isEmpty());

    // plugin output is attacker-controlled: it must stay one quoted argument, even when it contains
    // quotes or another placeholder's name
    QString evil = "x'; touch /tmp/pwn; echo '$SERVER$";
    CHECK(expandCommand("ssh $HOST$ $STATUS-INFO$ $SERVER$", {{"HOST", "h1"}, {"STATUS-INFO", evil}, {"SERVER", "a b"}}) ==
          "ssh 'h1' 'x'\\''; touch /tmp/pwn; echo '\\''$SERVER$' 'a b'");

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
    CHECK(out[0].passive && out[0].flapping && out[0].downtime && !out[0].hard &&
          out[0].output == "a\nb" && out[0].displayOutput() == "a b");
    CHECK(!parseStatusJson("<html>", true, "s", false, &out));

    // multi-column sort: host ascending, then service descending within the same host
    QVector<Item> l{svc("b", "s1", WARNING), svc("a", "s2", CRITICAL), svc("A", "S1", WARNING)};
    QVector<SortKey> keys{{BY_HOST, false}, {BY_SERVICE, true}};
    std::stable_sort(l.begin(), l.end(), [&](const Item &x, const Item &y) { return itemLess(x, y, keys); });
    CHECK(l[0].service == "s2" && l[1].service == "S1" && l[2].host == "b");
    CHECK(!itemLess(l[0], l[0], keys) && !itemLess(l[0], l[1], {}));
}

static void configTests() {
    QTemporaryDir directory;
    CHECK(directory.isValid());
    const QByteArray oldPath = qgetenv("NAFTAMON_CONFIG");
    qputenv("NAFTAMON_CONFIG", (directory.path() + "/gateway.ini").toUtf8());
    AppConfig defaults;
    defaults.load();
    CHECK(!defaults.webhookEnabled && defaults.webhookUrl.isEmpty() && defaults.webhookIntervalSeconds == 60);
    defaults.webhookEnabled = true;
    defaults.webhookUrl = "http://webhooks:8080/webhook";
    defaults.webhookIntervalSeconds = 17;
    defaults.save();
    AppConfig loaded;
    loaded.load();
    CHECK(loaded.webhookEnabled && loaded.webhookUrl == defaults.webhookUrl && loaded.webhookIntervalSeconds == 17);
    QSettings settings(AppConfig::path(), QSettings::IniFormat);
    CHECK(settings.value("webhook_enabled").toBool());
    settings.setValue("webhook_interval_seconds", 999999);
    settings.sync();
    AppConfig clamped;
    clamped.load();
    CHECK(clamped.webhookIntervalSeconds == 86400);
    settings.setValue("webhook_interval_seconds", -1);
    settings.sync();
    clamped.load();
    CHECK(clamped.webhookIntervalSeconds == 1);
    if (oldPath.isEmpty()) qunsetenv("NAFTAMON_CONFIG");
    else qputenv("NAFTAMON_CONFIG", oldPath);
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

static bool waitUntil(std::function<bool()> condition, int ms) {
    QElapsedTimer elapsed; elapsed.start();
    while (!condition() && elapsed.elapsed() < ms) {
        QEventLoop loop;
        QTimer::singleShot(25, &loop, &QEventLoop::quit);
        loop.exec();
    }
    return condition();
}

static QJsonDocument sinkGet(const QString &base, const QString &path) {
    QNetworkAccessManager manager;
    QNetworkRequest request(QUrl(base + path));
    request.setTransferTimeout(2000);
    QNetworkReply *reply = manager.get(request);
    QEventLoop loop;
    QObject::connect(reply, &QNetworkReply::finished, &loop, &QEventLoop::quit);
    QTimer::singleShot(2500, &loop, &QEventLoop::quit);
    loop.exec();
    QJsonDocument result = QJsonDocument::fromJson(reply->readAll());
    reply->deleteLater();
    return result;
}
static QJsonArray receivedWebhooks(const QString &base) { return sinkGet(base, "/received").array(); }
static QJsonObject sinkStats(const QString &base) { return sinkGet(base, "/stats").object(); }
static QJsonObject mockStats(const QString &url);

static void webhookTests(const QString &thrukUrl, const QString &sinkUrl) {
    QUrl receivedUrl(sinkUrl);
    receivedUrl.setPath("");
    receivedUrl.setQuery(QString());
    const QString base = receivedUrl.toString();
    ServerConf conf{"gateway-test", thrukUrl, "admin", "secret", {}};
    ThrukServer thruk(conf);
    const int fullQueriesBefore = mockStats(thrukUrl).value("full").toInt();
    thruk.refresh();
    CHECK(waitFor(thruk, [&] { return thruk.hasData || !thruk.error.isEmpty(); }, 5000));
    CHECK(thruk.error.isEmpty());
    CHECK(thruk.raw.hosts.size() == 1 && thruk.raw.services.size() == 3);
    CHECK(thruk.raw.okStatuses.size() == 2);
    CHECK(mockStats(thrukUrl).value("full").toInt() - fullQueriesBefore == 2);
    const QString specialInfo = QString::fromUtf8("  café — <disk> &\npath=\"/var/lib\" ✓  ");
    const QString hostInfo = QString::fromUtf8("PING CRITICAL — café & <host> ✓");
    if (!thruk.raw.services.isEmpty()) {
        thruk.raw.services[0].output = specialInfo;
        thruk.raw.services[0].downtime = true;
        thruk.raw.services[1].output = QString();
    }
    if (!thruk.raw.hosts.isEmpty()) thruk.raw.hosts[0].output = hostInfo;
    ThrukServer empty(ServerConf{"empty", thrukUrl, "admin", "secret", {}});
    empty.hasData = true;
    ThrukServer broken(ServerConf{"broken", thrukUrl, "admin", "secret", {}});
    broken.hasData = true;  // a previous successful poll must not be treated as current
    broken.raw = thruk.raw;
    broken.error = "connection failed";
    ThrukServer pending(ServerConf{"pending", thrukUrl, "admin", "secret", {}});

    WebhookGateway gateway(nullptr, 250);
    gateway.setServers({&thruk, &empty});
    int delivered = 0, failed = 0, lastStatus = -1;
    QObject::connect(&gateway, &WebhookGateway::delivered, &gateway, [&](int status) {
        ++delivered; lastStatus = status;
    });
    QObject::connect(&gateway, &WebhookGateway::deliveryFailed, &gateway, [&](int status) {
        ++failed; lastStatus = status;
    });
    const int before = sinkStats(base).value("total").toInt();
    CHECK(!gateway.isEnabled());
    CHECK(!waitUntil([&] { return sinkStats(base).value("total").toInt() > before; }, 350));

    gateway.configure(true, sinkUrl, 1);
    CHECK(waitUntil([&] { return delivered >= 1; }, 2000));
    CHECK(lastStatus == 202);
    QJsonArray records = receivedWebhooks(base);
    CHECK(sinkStats(base).value("total").toInt() > before && !records.isEmpty());
    QJsonObject payload = records.last().toObject();
    CHECK(payload.value("source") == "NaftaMon" && payload.value("complete") == true);
    CHECK(QDateTime::fromString(payload.value("timestamp").toString(), Qt::ISODateWithMs).isValid());
    CHECK(payload.value("servers").toArray().size() == 2);
    CHECK(payload.value("alerts").toArray().size() == 4);
    CHECK(payload.value("ok_statuses").toArray().size() == 2);
    bool acknowledged = false, downtime = false, foundHostInfo = false, foundEmptyServiceInfo = false,
         foundSpecialInfo = false;
    for (const auto &v : payload.value("alerts").toArray()) {
        QJsonObject a = v.toObject();
        CHECK(a.value("server") == "gateway-test" && !a.value("state").toString().isEmpty());
        CHECK(a.value("state") != "UP");
        CHECK(a.value("status_information").isString());
        CHECK(QDateTime::fromString(a.value("last_check").toString(), Qt::ISODate).isValid());
        acknowledged |= a.value("acknowledged").toBool();
        downtime |= a.value("downtime").toBool();
        if (a.value("type") == "host") {
            foundHostInfo = true;
            CHECK(a.value("status_information").toString() == hostInfo);
        }
        if (a.value("host") == "db01" && a.value("service") == "load") {
            foundEmptyServiceInfo = true;
            CHECK(a.value("status_information").toString().isEmpty());
        }
        if (a.value("host") == "db01" && a.value("service") == "disk") {
            foundSpecialInfo = true;
            CHECK(a.value("status_information").toString() == specialInfo);
        }
    }
    CHECK(foundHostInfo && foundEmptyServiceInfo && foundSpecialInfo);
    bool foundOkHost = false, foundOkService = false;
    for (const auto &v : payload.value("ok_statuses").toArray()) {
        const QJsonObject item = v.toObject();
        CHECK(item.value("state") == "UP" && item.value("status_information").isString());
        CHECK(item.value("server") == "gateway-test" && item.value("backend") == "alpha");
        foundOkHost |= item.value("type") == "host" && item.value("host") == "maint01" &&
                       item.value("status_information") == "PING OK";
        foundOkService |= item.value("type") == "service" && item.value("host") == "web01" &&
                          item.value("service") == "ssh" &&
                          item.value("status_information").toString() == "OK – café & <ready> ✓";
    }
    CHECK(foundOkHost && foundOkService);
    CHECK(acknowledged && downtime);  // visual acknowledgement/downtime filters do not apply
    const QByteArray body = QJsonDocument(payload).toJson(QJsonDocument::Compact);
    CHECK(!body.contains("secret") && !body.contains("admin") && !body.contains("thrukUrl") &&
          !body.contains("password") && !body.contains("token"));

    CHECK(waitUntil([&] { return delivered >= 2; }, 1700));  // one-second interval
    gateway.configure(true, sinkUrl, 2);
    CHECK(waitUntil([&] { return delivered >= 3; }, 1000));  // immediate on reconfigure
    const int slowIntervalStart = delivered;
    CHECK(!waitUntil([&] { return delivered > slowIntervalStart; }, 1100));
    CHECK(waitUntil([&] { return delivered > slowIntervalStart; }, 1400));

    gateway.setServers({&empty});
    CHECK(waitUntil([&] { return delivered >= slowIntervalStart + 2; }, 1000));
    payload = receivedWebhooks(base).last().toObject();
    CHECK(payload.value("complete") == true && payload.value("alerts").toArray().isEmpty());
    CHECK(payload.value("servers").toArray().first().toObject().value("status") == "ok");

    gateway.setServers({&empty, &broken});
    CHECK(waitUntil([&] { return delivered >= slowIntervalStart + 3; }, 1000));
    payload = receivedWebhooks(base).last().toObject();
    CHECK(payload.value("complete") == false && payload.value("alerts").toArray().isEmpty());
    CHECK(payload.value("ok_statuses").toArray().isEmpty());
    CHECK(payload.value("servers").toArray().last().toObject().value("status") == "error");

    const int beforePartial = delivered;
    gateway.setServers({&thruk, &broken, &pending});
    CHECK(waitUntil([&] { return delivered > beforePartial; }, 1000));
    payload = receivedWebhooks(base).last().toObject();
    CHECK(payload.value("complete") == false && payload.value("alerts").toArray().size() == 4);
    CHECK(payload.value("ok_statuses").toArray().size() == 2);
    CHECK(payload.value("servers").toArray().last().toObject().value("status") == "pending");

    ThrukServer second(ServerConf{"gateway-second", thrukUrl, "admin", "secret", {}});
    second.hasData = true;
    second.raw = thruk.raw;
    for (Item &item : second.raw.hosts) item.server = second.conf.name;
    for (Item &item : second.raw.services) item.server = second.conf.name;
    for (Item &item : second.raw.okStatuses) item.server = second.conf.name;
    const int beforeMulti = delivered;
    gateway.setServers({&thruk, &second});
    CHECK(waitUntil([&] { return delivered > beforeMulti; }, 1000));
    payload = receivedWebhooks(base).last().toObject();
    CHECK(payload.value("complete") == true && payload.value("servers").toArray().size() == 2);
    CHECK(payload.value("alerts").toArray().size() == 8);
    QSet<QString> alertServers;
    for (const auto &value : payload.value("alerts").toArray())
        alertServers.insert(value.toObject().value("server").toString());
    CHECK(alertServers == QSet<QString>({"gateway-test", "gateway-second"}));

    // A strict legacy receiver rejects the additions once, then keeps receiving
    // the original payload shape without generating or losing alert snapshots.
    const QString legacyUrl = base + "/legacy";
    WebhookGateway compatibility(nullptr, 1000);
    compatibility.setServers({&thruk});
    int compatibilityDelivered = 0;
    QObject::connect(&compatibility, &WebhookGateway::delivered, &compatibility,
                     [&](int) { ++compatibilityDelivered; });
    const int legacyRequestsBefore = sinkStats(base).value("total").toInt();
    const int rejectedBefore = sinkStats(base).value("extended_rejected").toInt();
    compatibility.configure(true, legacyUrl, 60);
    CHECK(waitUntil([&] { return compatibilityDelivered == 1; }, 1500));
    CHECK(sinkStats(base).value("total").toInt() == legacyRequestsBefore + 2);
    CHECK(sinkStats(base).value("extended_rejected").toInt() == rejectedBefore + 1);
    payload = receivedWebhooks(base).last().toObject();
    CHECK(!payload.contains("ok_statuses"));
    for (const auto &v : payload.value("alerts").toArray())
        CHECK(!v.toObject().contains("status_information"));
    const int legacyRequestsAfterFallback = sinkStats(base).value("total").toInt();
    compatibility.sendNow();
    CHECK(waitUntil([&] { return compatibilityDelivered == 2; }, 1000));
    CHECK(sinkStats(base).value("total").toInt() == legacyRequestsAfterFallback + 1);
    compatibility.configure(false, legacyUrl, 60);

    gateway.configure(true, sinkUrl + "?status=500", 1);
    CHECK(waitUntil([&] { return failed >= 1; }, 1500));
    CHECK(lastStatus == 500);
    const int generic422Before = sinkStats(base).value("total").toInt();
    gateway.configure(true, sinkUrl + "?status=422", 1);
    CHECK(waitUntil([&] { return failed >= 2; }, 1500));
    CHECK(lastStatus == 422);
    CHECK(sinkStats(base).value("total").toInt() == generic422Before + 1);
    gateway.configure(true, "http://127.0.0.1:1/webhook", 1);
    CHECK(waitUntil([&] { return failed >= 3; }, 1500));
    CHECK(lastStatus == 0);
    gateway.configure(true, sinkUrl + "?delay=2&status=500", 1);
    CHECK(waitUntil([&] { return failed >= 4; }, 1500));
    CHECK(lastStatus == 0);  // absolute 250 ms timeout
    gateway.configure(false, sinkUrl, 1);
    const int stoppedAt = sinkStats(base).value("total").toInt();
    CHECK(!waitUntil([&] { return sinkStats(base).value("total").toInt() > stoppedAt; }, 1300));
    CHECK(!gateway.isEnabled());

    CHECK(!WebhookGateway::isValidUrl(""));
    CHECK(!WebhookGateway::isValidUrl("ftp://webhooks/webhook"));
    CHECK(!WebhookGateway::isValidUrl("http://user:password@webhooks/webhook"));
    CHECK(WebhookGateway::isValidUrl(sinkUrl));
    int testDone = 0, testStatus = -1;
    QString testError;
    QString testResponse;
    QObject::connect(&gateway, &WebhookGateway::requestFinished, &gateway,
                     [&](const QDateTime &when, bool test, int status, const QString &error,
                         const QString &response) {
        if (!test) return;
        CHECK(when.isValid());
        ++testDone;
        testStatus = status;
        testError = error;
        testResponse = response;
    });
    CHECK(!gateway.testWebhook("invalid URL"));
    CHECK(gateway.testWebhook(sinkUrl));  // test works even while periodic delivery is disabled
    CHECK(waitUntil([&] { return testDone == 1; }, 1500));
    CHECK(testStatus == 202 && testError.isEmpty() && testResponse.contains("received"));
    payload = receivedWebhooks(base).last().toObject();
    CHECK(payload.value("type") == "test" && payload.value("test") == true);
    CHECK(payload.value("source") == "NaftaMon" && !payload.contains("alerts"));
    CHECK(!QJsonDocument(payload).toJson().contains("secret"));
    CHECK(gateway.testWebhook(sinkUrl + "?status=500"));
    CHECK(waitUntil([&] { return testDone == 2; }, 1500));
    CHECK(testStatus == 500 && !testError.isEmpty() && testResponse.contains("received"));
    CHECK(gateway.testWebhook("http://127.0.0.1:1/webhook"));
    CHECK(waitUntil([&] { return testDone == 3; }, 1500));
    CHECK(testStatus == 0 && !testError.isEmpty());
    CHECK(gateway.testWebhook(sinkUrl + "?delay=2&status=500"));
    CHECK(!gateway.testWebhook(sinkUrl));  // at most one manual test request in progress
    CHECK(waitUntil([&] { return testDone == 4; }, 1500));
    CHECK(testStatus == 0 && !testError.isEmpty());
    CHECK(!gateway.isEnabled());

    ThrukServer bulk(ServerConf{"bulk", thrukUrl, "admin", "secret", {}});
    bulk.hasData = true;
    for (int n = 0; n < 7000; ++n) {
        Item item = svc("bulk-host-" + QString::number(n) + QString(100, 'x'), "disk", CRITICAL);
        item.server = "bulk";
        bulk.raw.services.append(item);
        Item healthy = svc("healthy-host-" + QString::number(n) + QString(100, 'y'), "ping", UP);
        healthy.server = "bulk";
        healthy.output = QString(100, 'z');
        bulk.raw.okStatuses.append(healthy);
    }
    WebhookGateway large(nullptr, 5000);
    large.setServers({&bulk});
    int largeDelivered = 0;
    QObject::connect(&large, &WebhookGateway::delivered, &large, [&](int) { ++largeDelivered; });
    large.configure(true, sinkUrl + "?quiet=1", 60);
    CHECK(waitUntil([&] { return largeDelivered == 1; }, 5000));
    large.configure(false, sinkUrl, 60);
    const int largeBytes = sinkStats(base).value("last_body_bytes").toInt();
    CHECK(largeBytes > 1024 * 1024 && largeBytes < 8 * 1024 * 1024);
    const QJsonObject largePayload = receivedWebhooks(base).last().toObject();
    CHECK(largePayload.value("alerts").toArray().size() == 7000);
    CHECK(largePayload.value("ok_statuses").toArray().size() == 7000);

    WebhookGateway slow(nullptr, 4000);
    slow.setServers({&empty});
    const int slowBefore = sinkStats(base).value("total").toInt();
    slow.configure(true, sinkUrl + "?delay=2.5&status=500", 1);
    CHECK(waitUntil([&] { return sinkStats(base).value("total").toInt() > slowBefore; }, 1000));
    CHECK(!waitUntil([&] { return sinkStats(base).value("total").toInt() > slowBefore + 1; }, 1300));
    slow.configure(false, sinkUrl, 1);
    CHECK(!waitUntil([&] { return sinkStats(base).value("total").toInt() > slowBefore + 1; }, 1200));
}

static void gatewayUiTests(const QString &thrukUrl, const QString &sinkUrl) {
    QTemporaryDir directory;
    CHECK(directory.isValid());
    const QByteArray oldPath = qgetenv("NAFTAMON_CONFIG");
    qputenv("NAFTAMON_CONFIG", (directory.path() + "/ui.ini").toUtf8());
    AppConfig initial;
    initial.servers.append(ServerConf{"ui-mock", thrukUrl, "admin", "secret", {}});
    initial.floatingBar = false;
    initial.showAtStart = true;
    initial.notify = false;
    initial.sound = false;
    initial.save();
    QUrl receivedUrl(sinkUrl);
    receivedUrl.setPath("");
    receivedUrl.setQuery(QString());
    const QString base = receivedUrl.toString();
    {
        App app;
        QWidget *window = nullptr;
        for (QWidget *candidate : QApplication::topLevelWidgets())
            if (candidate->findChild<QToolButton *>("gatewayToggle")) window = candidate;
        CHECK(window != nullptr);
        if (window) {
            auto *toggle = window->findChild<QToolButton *>("gatewayToggle");
            auto *settings = window->findChild<QToolButton *>("gatewaySettings");
            auto *last = window->findChild<QLabel *>("gatewayLastResult");
            auto *table = window->findChild<QTableView *>();
            CHECK(toggle && settings && last && table);
            if (toggle && settings && last && table) {
                CHECK(!toggle->isChecked() && toggle->text().contains("Disabled"));
                CHECK(waitUntil([&] { return table->model()->rowCount() == 4; }, 3000));
                const int rows = table->model()->rowCount();
                QTimer::singleShot(0, &app, [&] {
                    auto *dialog = qobject_cast<QDialog *>(QApplication::activeModalWidget());
                    CHECK(dialog && dialog->objectName() == "gatewayDialog");
                    if (!dialog) return;
                    auto *url = dialog->findChild<QLineEdit *>("gatewayUrl");
                    auto *interval = dialog->findChild<QSpinBox *>("gatewayInterval");
                    auto *test = dialog->findChild<QPushButton *>("gatewayTest");
                    auto *result = dialog->findChild<QPlainTextEdit *>("gatewayTestResult");
                    auto *buttons = dialog->findChild<QDialogButtonBox *>();
                    CHECK(url && interval && test && result && buttons);
                    if (!url || !interval || !test || !result || !buttons) { dialog->reject(); return; }
                    url->setText(sinkUrl);
                    interval->setValue(1);
                    test->click();
                    CHECK(!test->isEnabled() && result->toPlainText().contains("Sending"));
                    CHECK(waitUntil([&] { return result->toPlainText().contains("HTTP 202"); }, 2000));
                    CHECK(result->toPlainText().contains("Response:") && result->toPlainText().contains("received"));
                    CHECK(result->maximumHeight() <= 120 && result->isReadOnly());
                    CHECK(test->isEnabled());
                    const QString shot = qEnvironmentVariable("GATEWAY_DIALOG_SCREENSHOT");
                    if (!shot.isEmpty()) CHECK(dialog->grab().save(shot));
                    url->setText(sinkUrl + "?status=500");
                    test->click();
                    CHECK(waitUntil([&] { return result->toPlainText().contains("HTTP 500"); }, 2000));
                    CHECK(result->toPlainText().contains("Response:") && result->toPlainText().contains("received"));
                    url->setText(sinkUrl);
                    buttons->button(QDialogButtonBox::Save)->click();
                });
                settings->click();
                AppConfig saved;
                saved.load();
                CHECK(saved.webhookUrl == sinkUrl && saved.webhookIntervalSeconds == 1);
                CHECK(!saved.webhookEnabled && last->text().contains("Test: HTTP 500"));
                QJsonObject testPayload = receivedWebhooks(base).last().toObject();
                CHECK(testPayload.value("test") == true && testPayload.value("type") == "test");
                QTimer::singleShot(0, &app, [&] {
                    auto *dialog = qobject_cast<QDialog *>(QApplication::activeModalWidget());
                    CHECK(dialog != nullptr);
                    if (!dialog) return;
                    dialog->findChild<QLineEdit *>("gatewayUrl")->setText(sinkUrl + "?delay=0.4&status=500");
                    dialog->findChild<QPushButton *>("gatewayTest")->click();
                    dialog->reject();
                });
                settings->click();
                QTimer::singleShot(0, &app, [&] {
                    auto *dialog = qobject_cast<QDialog *>(QApplication::activeModalWidget());
                    CHECK(dialog != nullptr);
                    if (!dialog) return;
                    auto *result = dialog->findChild<QPlainTextEdit *>("gatewayTestResult");
                    const QString initialText = result->toPlainText();
                    CHECK(!waitUntil([&] { return result->toPlainText() != initialText; }, 700));
                    dialog->reject();
                });
                settings->click();
                const int before = sinkStats(base).value("total").toInt();
                toggle->click();
                CHECK(toggle->isChecked() && toggle->text().contains("Enabled"));
                CHECK(waitUntil([&] { return sinkStats(base).value("total").toInt() > before; }, 2000));
                QJsonObject alertPayload = receivedWebhooks(base).last().toObject();
                CHECK(alertPayload.value("source") == "NaftaMon" && alertPayload.value("test").isUndefined());
                CHECK(alertPayload.value("complete") == true && alertPayload.value("alerts").toArray().size() == 4);
                CHECK(table->model()->rowCount() == rows);  // Gateway does not change the monitor table
                saved = AppConfig();
                saved.load();
                CHECK(saved.webhookEnabled);
                toggle->click();
                CHECK(!toggle->isChecked() && toggle->text().contains("Disabled"));
                const int stoppedAt = sinkStats(base).value("total").toInt();
                CHECK(!waitUntil([&] { return sinkStats(base).value("total").toInt() > stoppedAt; }, 1200));
                saved = AppConfig();
                saved.load();
                CHECK(!saved.webhookEnabled);
                CHECK(table->model()->rowCount() == rows);
                QTimer::singleShot(0, &app, [&] {
                    auto *dialog = qobject_cast<QDialog *>(QApplication::activeModalWidget());
                    CHECK(dialog != nullptr);
                    if (!dialog) return;
                    dialog->findChild<QLineEdit *>("gatewayUrl")->setText(sinkUrl + "?delay=2");
                    dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Save)->click();
                });
                settings->click();
                const int beforeSlow = sinkStats(base).value("total").toInt();
                toggle->click();
                CHECK(waitUntil([&] { return sinkStats(base).value("total").toInt() > beforeSlow; }, 1000));
                toggle->click();
                CHECK(last->text().contains("Cancelled") && !last->text().contains("Connection error"));
            }
        }
    }
    if (oldPath.isEmpty()) qunsetenv("NAFTAMON_CONFIG");
    else qputenv("NAFTAMON_CONFIG", oldPath);
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
    CHECK(diskp && diskp->state == CRITICAL && diskp->backend == "alpha");

    // backend list for the settings checklist, and hiding one of them
    QVector<Backend> backends;
    s.fetchBackends([&](QVector<Backend> list, QString) { backends = list; });
    CHECK(waitFor(s, [&] { return !backends.isEmpty(); }, 3000));
    CHECK(backends.size() == 2 && backends[1].id == "b2" && backends[1].name == "beta");
    ServerConf hc = c;
    hc.disabledBackends = "b2";
    ThrukServer hidden(hc);
    hidden.refresh();
    CHECK(waitFor(hidden, [&] { return hidden.hasData; }, 5000));
    CHECK(hidden.raw.services.size() == 2 && !find(hidden, "app01", "http"));
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
    // maintenance: all checks of a host off (listed as disabled, comment added), then on again
    Item maint;
    maint.server = "m";
    maint.host = "maint01";
    auto disabledHosts = [&] {
        QVector<Item> got;
        bool answered = false;
        s.fetchDisabledHosts([&](QVector<Item> l, QString) { got = l; answered = true; });
        waitFor(s, [&] { return answered; }, 3000);
        return got;
    };
    CHECK(disabledHosts().isEmpty());
    s.setHostChecks(maint, false, "kernel update");
    CHECK(waitFor(s, [&] { return mockStats(url)["cmd"].toObject().contains("1"); }, 3000));
    QJsonObject cmds = mockStats(url)["cmd"].toObject();
    CHECK(cmds["16"].toObject()["ahas"].toString() == "on");
    CHECK(cmds["1"].toObject()["com_data"].toString() == "Maintenance: kernel update");
    QVector<Item> dis = disabledHosts();
    CHECK(dis.size() == 1 && dis[0].host == "maint01" && dis[0].passive && dis[0].backend == "alpha");
    s.setHostChecks(dis.value(0, maint), true);
    CHECK(waitFor(s, [&] { return mockStats(url)["cmd"].toObject().contains("15"); }, 3000));
    CHECK(waitFor(s, [&] { return disabledHosts().isEmpty(); }, 3000));

    CHECK(failed.isEmpty());
    if (!failed.isEmpty()) std::fprintf(stderr, "command failed: %s\n", qPrintable(failed));

    ServerConf down{"m", "http://127.0.0.1:1/thruk", "admin", "secret", {}};
    ThrukServer d(down);
    d.refresh();
    CHECK(waitFor(d, [&] { return !d.error.isEmpty(); }, 5000));
}

int main(int argc, char **argv) {
    QApplication app(argc, argv);
    app.setApplicationName("naftamon-tests");
    app.setQuitOnLastWindowClosed(false);
    coreTests();
    configTests();
    QString url = qEnvironmentVariable("MOCK_URL");
    QString webhookUrl = qEnvironmentVariable("WEBHOOK_URL");
    if (!url.isEmpty() && !webhookUrl.isEmpty()) webhookTests(url, webhookUrl);
    if (!url.isEmpty() && !webhookUrl.isEmpty()) gatewayUiTests(url, webhookUrl);
    if (!url.isEmpty()) thrukTests(url);
    std::printf(failures ? "%d FAILURE(S)\n" : "all tests passed\n", failures);
    return failures ? 1 : 0;
}
