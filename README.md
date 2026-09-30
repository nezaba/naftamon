# Naftamon

Native (C++/Qt6) Linux replacement for Nagstamon, for Thruk servers.

## Install (Arch or Debian/Ubuntu)

    ./install.sh && ~/.local/bin/naftamon

## Switching from Nagstamon

Naftamon imports your Nagstamon configuration, so there is nothing to set up again. On the first start it finds
`~/.nagstamon` and offers the import; later, use **Settings → Servers → Import from Nagstamon…** (if the folder is
elsewhere, e.g. a copy from another machine, it asks for it). Nagstamon's files are only read, both apps can run side
by side.

Imported:

* **Thruk servers** with URL, username, password, disabled backends, "ignore certificate" and "display name" options.
  The login is decoded from Nagstamon's obfuscated format; if it cannot be read (password not saved, or kept in
  Nagstamon's keyring) the server dialog opens with everything else filled in.
* **The same Thruk added several times** with different disabled backends (a common workaround in Nagstamon) can be
  merged into one server: backends shown by any copy stay shown, and the Backend column tells them apart.
* **Filters, regex filters, notification settings and colors**: Naftamon uses Nagstamon's option names.
* **Custom actions** of type command (`$ADDRESS$` becomes `$HOST$`); Nagstamon's unchanged default actions are left out.

Not imported: other monitor types (listed in the import dialog), browser/URL actions, and the update interval
(Naftamon keeps 10 s instead of Nagstamon's 60 s).

## Build & run

    mkdir -p build && cd build && qmake6 ../naftamon.pro && make
    ./naftamon

Needs Qt6 Widgets, Network, Multimedia (no other dependencies). Server URL: the same "Monitor CGI URL" as in Nagstamon, e.g. `http://host/thruk/cgi-bin`.
Config: `~/.config/naftamon/naftamon.ini`
(mode 0600, passwords obfuscated like Nagstamon does, not encrypted), or the path in `$NAFTAMON_CONFIG`.

Tests (core logic + Thruk client against a mock Thruk): `tests/run.sh`

## Why it is faster than Nagstamon

Measured cause of the slow "recheck" in Nagstamon (source read at commit ed379f9):

* it polls every 60 s by default and does **not** refresh after a command; the fixed alert stays until the next poll.
* it never sends `json`/`referer` to `cmd.cgi`, so Thruk's `use_wait_feature` (on by default, `wait_timeout=10`) is not used.
* it sends no `CSRFtoken`; current Thruk rejects command POSTs without it unless the client is in `csrf_allowed_hosts`.

Naftamon:

* sends commands with `CSRFtoken` and `json=1`, so Thruk replies once the check result is in, then re-polls immediately;
* after a recheck polls only the affected host every 250 ms (`status.cgi?host=H`, a few rows, max 30 s) until the item has a newer `last_check`, then does one full refresh; rows show "⟳ rechecking…" meanwhile;
* re-polls immediately after acknowledge / downtime / submit result;
* fetches hosts and services in parallel; default interval 10 s (configurable, 1 s minimum).

## Same as Nagstamon

Same Thruk endpoints and columns (`status.cgi … view_mode=json`, `login.cgi` cookie login, `cmd.cgi` types 7/96, 33/34,
55/56, 30/87), same filters, same regex filters (host, service, status information, duration, attempt), same
"new problem" detection and notification rules (notify_if_*, escalate only to worse state, stop when the status window
is opened), same default colors and default sort (status descending). Actions: monitor (open in browser), recheck,
recheck all, acknowledge (sticky/notify/persistent/all services), downtime (fixed/flexible, times from the server's form),
submit check result, copy. Floating status bar, tray icon, sound, flashing, desktop notification, notification actions.

## Extras

* Status chips with counts next to the search box: click one to show only that state.
* Quick filter buttons Acknowledged / Downtime / Flapping / New: pressed = hidden, saved. They are also the legend
  of the row badges.
* Search box (Ctrl+F, Esc clears), shortcuts R (recheck), A (acknowledge), D (downtime), F5 (refresh).
* Recheck all services on host (`cmd_typ=17`, forced), remove acknowledgement (`cmd_typ=51/52`).
* Acknowledge with expiry (`use_expire` + `expire_time`); only honoured if the core supports expiring acks (Naemon, Icinga).
* Custom actions (Settings → Actions), default "SSH" = `ssh $HOST$` in a terminal. Placeholders are shell-quoted
  (Nagstamon inserts them raw). Terminal: `$TERMINAL`, else konsole, gnome-terminal, kgx, xfce4-terminal, alacritty,
  kitty, foot, xterm.
* New problems are bold with flag `N`: their state changed (`last_state_change`) after the status window was last
  closed, or after startup. Nagstamon marks everything the app has not seen yet, so all old problems are "new" at start.

## Deliberate differences

* Service "flapping" uses Thruk's `is_flapping` (Nagstamon uses `notifications_enabled` there, a bug).
* Host query: only DOWN/UNREACHABLE hosts (`hoststatustypes=12`). Nagstamon also ORs in every host in downtime,
  acknowledged, soft or with notifications/checks disabled (thousands on big setups, every poll); the flags of a
  problem service's host come with the service row instead (`host_state`, `host_acknowledged`, ...).
* Sound repeat actually repeats (Nagstamon's repeat condition can never be true).
* OK action runs when a server recovers to all-OK, not on every refresh.
* All servers in one table with a Backend column (Thruk `peer_name`; with several servers it reads `server · backend`).
* Backends are picked from a checklist in the server dialog (list from Thruk's REST `/r/sites`, Thruk 2.24+). The
  unchecked ones are sent as the `thruk_backends` cookie, like Nagstamon's "disabled backends"; if the list cannot
  be loaded, the ids can still be typed.
  The ▾ button in the Backend column header offers the same checklist without opening Settings.
* Header clicks sort by several columns at once: each column cycles ascending, descending, off; the first clicked
  is the primary one (the number next to the arrow).
* Built-in tones instead of Nagstamon's .wav files; custom sound files supported.
* Ack/downtime dialogs remember the last values instead of separate "defaults" settings.

## Not implemented (yet)

Other monitor types (Icinga, Checkmk, Zabbix…), browser/URL-type custom actions, $ADDRESS$ placeholder, proxies, system keyring,
autologin key, hover-to-open popup, fullscreen / windowed mode, hostgroup filter. On Wayland the compositor decides window positions (status bar can still be dragged).

## License

Copyright (C) 2026 nezaba

Naftamon is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any
later version. See [LICENSE](LICENSE).

Naftamon reimplements the behaviour of [Nagstamon](https://github.com/HenriWahl/Nagstamon) (GPL-2.0-or-later);
it contains no Nagstamon code.
