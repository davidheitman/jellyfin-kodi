#!/usr/bin/env python3
"""Re-authenticate Jellyfin for Kodi from a terminal.

For boxes with no practical way to reach the add-on's own dialogs: wall
mounted, headless, or simply a long way from a keyboard. Changing a Jellyfin
password invalidates the stored AccessToken, and the add-on's only route back
is a dialog on the television.

Runs under plain python3 with nothing but the standard library, and imports
nothing from the add-on, because the add-on imports Kodi modules that do not
exist outside Kodi.

    python3 tools/reauth.py --list
    python3 tools/reauth.py                     # password login
    python3 tools/reauth.py --quick-connect     # approve from a phone

Stop Kodi first. The add-on rewrites the whole file, so anything written
underneath a running Kodi is lost on its next save.
"""
import argparse
import getpass
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

#: Searched in order. LibreELEC and CoreELEC put userdata under /storage.
DATA_DIRS = (
    "/storage/.kodi/userdata/addon_data/plugin.video.jellyfin",
    "~/.kodi/userdata/addon_data/plugin.video.jellyfin",
    "~/.var/app/tv.kodi.Kodi/data/userdata/addon_data/plugin.video.jellyfin",
    "~/Library/Application Support/Kodi/userdata/addon_data/plugin.video.jellyfin",
    "~/AppData/Roaming/Kodi/userdata/addon_data/plugin.video.jellyfin",
)

CLIENT = "Jellyfin for Kodi"
VERSION = "1.0.0"


def find_data_dir(explicit=None):
    if explicit:
        path = os.path.expanduser(explicit)
        if not os.path.isfile(os.path.join(path, "data.json")):
            raise SystemExit("no data.json in %s" % path)
        return path

    for candidate in DATA_DIRS:
        path = os.path.expanduser(candidate)
        if os.path.isfile(os.path.join(path, "data.json")):
            return path

    raise SystemExit(
        "could not find data.json. Pass --data-dir with the directory holding it."
    )


def auth_header(device_id):
    # Jellyfin rejects an unauthenticated request without this. The values only
    # name the session in the server's device list.
    return 'MediaBrowser Client="%s", Device="%s", DeviceId="%s", Version="%s"' % (
        CLIENT,
        socket.gethostname(),
        device_id,
        VERSION,
    )


def request(server_url, path, device_id, payload=None, token=None, insecure=False):
    url = server_url.rstrip("/") + "/" + path.lstrip("/")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None

    header = auth_header(device_id)
    if token:
        header += ', Token="%s"' % token

    req = urllib.request.Request(url, data=body, method="POST" if body else "GET")
    req.add_header("Authorization", header)
    req.add_header("Accept", "application/json")
    if body:
        req.add_header("Content-Type", "application/json")

    context = None
    if insecure:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, timeout=15, context=context) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise SystemExit("%s %s: %s %s" % (req.get_method(), url, exc.code, detail))
    except urllib.error.URLError as exc:
        raise SystemExit("could not reach %s: %s" % (url, exc.reason))


def load(path):
    with open(path, "rb") as handle:
        return json.load(handle)


def save(path, document):
    """Same contract as the add-on's own writer: atomic, then owner only."""
    data = json.dumps(document, sort_keys=True, indent=4, ensure_ascii=False)
    temp = path + ".tmp"
    with open(temp, "wb") as handle:
        handle.write(data.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def describe(server):
    return "%s  %s  (user %s)" % (
        server.get("Name") or "?",
        server.get("address") or "?",
        server.get("UserId") or "none",
    )


def pick_server(servers, server_id=None):
    if not servers:
        raise SystemExit("no servers configured; set one up in Kodi first")

    if server_id:
        for server in servers:
            if server.get("Id") == server_id:
                return server
        raise SystemExit("no server with Id %s" % server_id)

    if len(servers) == 1:
        return servers[0]

    for index, server in enumerate(servers):
        print("  [%d] %s" % (index, describe(server)))
    try:
        return servers[int(input("server: ").strip())]
    except (ValueError, IndexError):
        raise SystemExit("not a listed server")


def password_login(server, device_id, args):
    username = args.username or input("username: ").strip()
    password = args.password
    if password is None:
        password = getpass.getpass("password: ")

    return request(
        server["address"],
        "Users/AuthenticateByName",
        device_id,
        payload={"Username": username, "Pw": password},
        insecure=args.insecure,
    )


def quick_connect_login(server, device_id, args):
    address = server["address"]
    started = request(
        address, "QuickConnect/Initiate", device_id, payload={}, insecure=args.insecure
    )
    secret, code = started.get("Secret"), started.get("Code")
    if not secret:
        raise SystemExit(
            "the server did not start a Quick Connect session. It is likely "
            "disabled; turn it on in Dashboard, General."
        )

    print("\n  Quick Connect code: %s\n" % code)
    print("Approve it from a signed in browser or app, under Quick Connect.")
    print("Waiting, Ctrl-C to give up.")

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        state = request(
            address,
            "QuickConnect/Connect?Secret=%s" % urllib.parse.quote(secret),
            device_id,
            insecure=args.insecure,
        )
        if state.get("Authenticated"):
            return request(
                address,
                "Users/AuthenticateWithQuickConnect",
                device_id,
                payload={"Secret": secret},
                insecure=args.insecure,
            )
        time.sleep(args.poll)

    raise SystemExit("nobody approved the code within %d seconds" % args.timeout)


def main():
    parser = argparse.ArgumentParser(
        description="Re-authenticate Jellyfin for Kodi without using the Kodi UI."
    )
    parser.add_argument("--data-dir", help="directory holding data.json")
    parser.add_argument("--server-id", help="which server, when several are configured")
    parser.add_argument("--username")
    parser.add_argument(
        "--password",
        help="prompted for when omitted, which keeps it out of shell history",
    )
    parser.add_argument(
        "--quick-connect",
        action="store_true",
        help="approve from another device instead of typing a password here",
    )
    parser.add_argument("--list", action="store_true", help="list servers and exit")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="skip TLS verification, for a self signed certificate",
    )
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--poll", type=float, default=3.0)
    args = parser.parse_args()

    path = os.path.join(find_data_dir(args.data_dir), "data.json")
    document = load(path)
    servers = document.get("Servers") or []

    if args.list:
        print(path)
        for server in servers:
            print("  %s  Id=%s" % (describe(server), server.get("Id")))
        return 0

    server = pick_server(servers, args.server_id)
    print("Authenticating against %s" % server.get("address"))

    device_id = server.get("Id") or socket.gethostname()
    result = (
        quick_connect_login(server, device_id, args)
        if args.quick_connect
        else password_login(server, device_id, args)
    )

    token = result.get("AccessToken")
    user = (result.get("User") or {}).get("Id")
    if not token or not user:
        raise SystemExit("the server did not return a token")

    # The same three fields connection_manager._save_auth_credentials writes.
    server["AccessToken"] = token
    server["UserId"] = user
    server["DateLastAccessed"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    save(path, document)

    print("Updated %s" % path)
    print("Start Kodi. Revoke the old session from Dashboard, Devices.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
