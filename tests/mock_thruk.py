#!/usr/bin/env python3
"""Minimal Thruk imitation for tests: cookie login, status.cgi JSON, cmd.cgi form + CSRF,
and the use_wait_feature behaviour (POST with json=1 blocks until the recheck has run).

usage: mock_thruk.py PORT [--no-wait] [--basic] [--big N]   (user admin / password secret)
--basic: every request needs a Basic Authorization header; 401 without a challenge otherwise
"""
import json, sys, threading, time, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

PORT = int(sys.argv[1])
WAIT_FEATURE = '--no-wait' not in sys.argv
BASIC = '--basic' in sys.argv
CHECK_DELAY = 1.0   # seconds a scheduled check needs to "run"
TOKEN = 'tok123'
lock = threading.Lock()
stats = {'full': 0, 'small': 0, 'cmd': {}}  # GET /thruk/_stats
now = lambda: int(time.time())

def svc(host, desc, state, **kw):
    d = dict(host_name=host, description=desc, display_name=desc, state=state, last_check=now() - 30,
             last_state_change=now() - 3700, plugin_output=f'{desc} output', current_attempt=3,
             max_check_attempts=3, active_checks_enabled=1, is_flapping=0, notifications_enabled=1,
             acknowledged=0, state_type=1, scheduled_downtime_depth=0, host_display_name=host,
             host_state=0, host_acknowledged=0, host_scheduled_downtime_depth=0, host_is_flapping=0,
             host_active_checks_enabled=1)
    d.update(kw)
    return d

hosts = {
    'web01': dict(name='web01', state=1, last_check=now() - 30, last_state_change=now() - 100,
                  plugin_output='PING CRITICAL', current_attempt=1, max_check_attempts=1,
                  active_checks_enabled=1, notifications_enabled=1, is_flapping=0, acknowledged=0,
                  scheduled_downtime_depth=0, state_type=1, host_display_name='web01', display_name='web01'),
}
services = {
    ('db01', 'disk'): svc('db01', 'disk', 2),
    ('db01', 'load'): svc('db01', 'load', 1, state_type=0, current_attempt=1),
    ('app01', 'http'): svc('app01', 'http', 3, acknowledged=1),
}

# --big N: add N problem services on N/10 hosts (performance tests)
if '--big' in sys.argv:
    n = int(sys.argv[sys.argv.index('--big') + 1])
    for i in range(n):
        h = f'bighost{i // 10:05d}.example.com'
        services[(h, f'svc{i % 10}')] = svc(h, f'svc{i % 10}', 1 + i % 3)

def run_check(key, is_host):
    time.sleep(CHECK_DELAY)
    with lock:
        objs = [hosts.get(key)] if is_host is True else \
               [v for k, v in services.items() if k[0] == key] if is_host == 'all_services' else [services.get(key)]
        for obj in filter(None, objs):
            obj['state'] = 0
            obj['last_check'] = now()

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, code, body, ctype='text/html', headers=()):
        b = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        for k, v in headers: self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def basic_ok(self):
        if BASIC and self.headers.get('Authorization') != 'Basic YWRtaW46c2VjcmV0':
            self.send(401, 'authentication required')
            return False
        return True

    def authed(self):
        return 'thruk_auth=abc' in (self.headers.get('Cookie') or '')

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == '/thruk/_stats':  # test helper, no auth
            with lock:
                return self.send(200, json.dumps(stats), 'application/json')
        if not self.basic_ok(): return
        if not u.path.startswith('/thruk'):
            return self.send(404, 'no')
        if not self.authed():
            return self.send(200, '<html><form action="login.cgi"><input name="login"></form></html>')
        if u.path.endswith('/status.cgi'):
            host = q.get('host', ['all'])[0]
            with lock:
                if host != 'all' and q.get('hostgroup', ['all'])[0] == 'all' and 'hostgroup' not in q:
                    # single-host query (Thruk: exact host filter, all states)
                    stats['small'] += 1
                    if q.get('style') == ['hostdetail']:
                        data = [h for h in hosts.values() if h['name'] == host]
                    else:
                        data = [s for s in services.values() if s['host_name'] == host]
                else:
                    stats['full'] += 1
                    if q.get('style') == ['hostdetail']:
                        assert q.get('hoststatustypes') == ['12']
                        data = [h for h in hosts.values() if h['state'] != 0]
                    else:
                        data = [s for s in services.values() if s['state'] != 0]
                return self.send(200, json.dumps(data), 'application/json')
        if u.path.endswith('/cmd.cgi'):
            t = time.strftime('%Y-%m-%d %H:%M:%S')
            e = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() + 7200))
            return self.send(200, f'<form><input type="hidden" name="CSRFtoken" value="{TOKEN}">'
                                  f'<input type="text" name="start_time" id="start_time" value="{t}">'
                                  f'<input type="text" name="end_time" value="{e}"></form>')
        return self.send(200, '<html>ok</html>')

    def do_POST(self):
        if not self.basic_ok(): return
        u = urllib.parse.urlparse(self.path)
        body = self.rfile.read(int(self.headers.get('Content-Length', 0))).decode()
        p = {k: v[0] for k, v in urllib.parse.parse_qs(body, keep_blank_values=True).items()}
        if u.path.endswith('/login.cgi'):
            if p.get('login') == 'admin' and p.get('password') == 'secret':
                return self.send(302, '', headers=[('Set-Cookie', 'thruk_auth=abc; path=/thruk/'), ('Location', '/thruk/')])
            return self.send(200, '<html>login failed</html>')
        if not self.authed():
            return self.send(200, '<html>login</html>')
        if u.path.endswith('/cmd.cgi'):
            if p.get('CSRFtoken') != TOKEN:
                return self.send(200, '<html>possible csrf, no or invalid token</html>')
            typ, host, service = int(p['cmd_typ']), p.get('host'), p.get('service')
            key = host if typ in (96, 33, 55, 87, 51, 17) else (host, service)
            is_host = typ in (96, 33, 55, 87, 51)
            with lock:
                stats['cmd'][str(typ)] = p
                if typ == 17:
                    threading.Thread(target=run_check, args=(host, 'all_services')).start()
                    return self.send(200, json.dumps({'success': 1}), 'application/json')
                obj = hosts.get(key) if is_host else services.get(key)
                if obj is None:
                    return self.send(200, json.dumps({'success': 0, 'error': 'no such object'}), 'application/json')
                if typ in (33, 34): obj['acknowledged'] = 1
                if typ in (51, 52): obj['acknowledged'] = 0
                if typ in (55, 56): obj['scheduled_downtime_depth'] = 1
                if typ in (87, 30): obj['state'] = int(p['plugin_state']); obj['last_check'] = now()
            if typ in (7, 96):
                t = threading.Thread(target=run_check, args=(key, is_host))
                t.start()
                if WAIT_FEATURE and p.get('json'):
                    t.join(10)  # Thruk wait_timeout
            if p.get('json'):
                return self.send(200, json.dumps({'success': 1}), 'application/json')
            return self.send(302, '', headers=[('Location', '/thruk/')])
        return self.send(404, 'no')

ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
