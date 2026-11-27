#!/usr/bin/env python3
"""Run a Jellyfin library sync from a terminal, with no Kodi interface.

The same operations the add-on offers under Manage Libraries: sync a library
for the first time, update one, repair one (which rewrites every item,
including the artwork URLs, so it is the fix after the server address
changes), or remove one.

This asks the running Kodi to do the work rather than doing it here. Every
module under jellyfin_kodi imports xbmc, xbmcgui, xbmcaddon, xbmcvfs or
xbmcplugin, none of which exist outside Kodi's own interpreter, so a
standalone process cannot drive a sync itself. Going through Kodi also means
this takes exactly the code path the on screen menu takes.

Kodi has to be running, with "Allow remote control via HTTP" switched on
under Settings, Services, Control.

    python3 tools/sync.py --list
    python3 tools/sync.py --repair <library-id>
    python3 tools/sync.py --repair-all
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _jfcli  # noqa: E402


def kodi_rpc(args, method, params):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1,
                          "method": method, "params": params}).encode("utf-8")
    url = "http://%s:%d/jsonrpc" % (args.kodi_host, args.kodi_port)

    request = urllib.request.Request(url, data=payload, method="POST")
    request.add_header("Content-Type", "application/json")
    if args.kodi_user:
        token = base64.b64encode(
            ("%s:%s" % (args.kodi_user, args.kodi_password or "")).encode("utf-8")
        ).decode("ascii")
        request.add_header("Authorization", "Basic " + token)

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise SystemExit(
                "Kodi refused the request. Pass --kodi-user and --kodi-password, "
                "or turn off the username requirement under Settings, Services, Control."
            )
        raise SystemExit("Kodi returned %s for %s" % (exc.code, method))
    except urllib.error.URLError as exc:
        raise SystemExit(
            "could not reach Kodi on %s (%s). Is it running, and is "
            "\"Allow remote control via HTTP\" on under Settings, Services, Control?"
            % (url, exc.reason)
        )

    if "error" in result:
        raise SystemExit("Kodi rejected %s: %s" % (method, result["error"]))
    return result.get("result")


def run_mode(args, mode, library_id=None):
    params = {"mode": mode}
    if library_id:
        params["id"] = library_id

    kodi_rpc(args, "Addons.ExecuteAddon",
             {"addonid": _jfcli.ADDON_ID, "params": params})


def libraries(args, data_dir):
    """Every view the server offers, flagged with whether Kodi has it."""
    document = _jfcli.load_json(os.path.join(data_dir, "data.json"))
    server = _jfcli.pick_server(document.get("Servers") or [], args.server_id)

    token, user = server.get("AccessToken"), server.get("UserId")
    if not token or not user:
        raise SystemExit("that server has no saved session; run reauth.py first")

    views = _jfcli.jellyfin_request(
        server["address"], "Users/%s/Views" % user,
        server.get("Id") or "kodi", token=token, insecure=args.insecure,
    )
    synced = set(_jfcli.load_json(os.path.join(data_dir, "sync.json"), {}).get("Whitelist") or [])

    rows = []
    for view in views.get("Items") or []:
        rows.append((view.get("Id"), view.get("Name"), view.get("CollectionType"),
                     view.get("Id") in synced))
    return rows


def main():
    parser = argparse.ArgumentParser(
        description="Sync, update, repair or remove Jellyfin libraries without the Kodi UI."
    )
    parser.add_argument("--data-dir", help="directory holding data.json")
    parser.add_argument("--server-id", help="which server, when several are configured")
    parser.add_argument("--kodi-host", default="127.0.0.1")
    parser.add_argument("--kodi-port", type=int, default=8080)
    parser.add_argument("--kodi-user")
    parser.add_argument("--kodi-password")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS verification, for a self signed certificate")

    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list", action="store_true", help="list libraries and exit")
    action.add_argument("--sync", metavar="ID", help="sync a library for the first time")
    action.add_argument("--update", metavar="ID", help="pull in what changed")
    action.add_argument("--repair", metavar="ID",
                        help="rewrite every item, including artwork URLs")
    action.add_argument("--remove", metavar="ID", help="remove a library from Kodi")
    action.add_argument("--repair-all", action="store_true",
                        help="repair every library Kodi has synced")
    action.add_argument("--update-all", action="store_true",
                        help="update every library Kodi has synced")
    args = parser.parse_args()

    data_dir = _jfcli.find_data_dir(args.data_dir)

    if args.list:
        print("%-34s %-26s %-10s %s" % ("ID", "NAME", "TYPE", "SYNCED"))
        for view_id, name, kind, is_synced in libraries(args, data_dir):
            print("%-34s %-26s %-10s %s"
                  % (view_id, (name or "")[:26], kind or "", "yes" if is_synced else "no"))
        return 0

    if args.repair_all or args.update_all:
        mode = "repairlib" if args.repair_all else "updatelib"
        targets = [(i, n) for i, n, _, synced in libraries(args, data_dir) if synced]
        if not targets:
            raise SystemExit("Kodi has no synced libraries to work on")
        for view_id, name in targets:
            print("%s: %s" % (mode, name))
            run_mode(args, mode, view_id)
    else:
        mode, library_id = next(
            (m, v) for m, v in (("synclib", args.sync), ("updatelib", args.update),
                                ("repairlib", args.repair), ("removelib", args.remove))
            if v
        )
        run_mode(args, mode, library_id)

    print("\nKodi has been asked to do the work; it runs in the background.")
    print("Watch it with: tail -f /storage/.kodi/temp/kodi.log | grep -i jellyfin")
    return 0


if __name__ == "__main__":
    sys.exit(main())
