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
import os
import sys
import time
import urllib.parse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _jfcli  # noqa: E402

find_data_dir = _jfcli.find_data_dir
describe = _jfcli.describe_server
pick_server = _jfcli.pick_server


def request(server_url, path, device_id, payload=None, token=None, insecure=False):
    return _jfcli.jellyfin_request(
        server_url, path, device_id, payload=payload, token=token, insecure=insecure
    )


def load(path):
    return _jfcli.load_json(path)


def save(path, document):
    _jfcli.save_json(path, document)


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
