"""Shared helpers for the terminal tools.

Deliberately imports nothing from the add-on: everything under jellyfin_kodi
imports xbmc, xbmcgui, xbmcaddon, xbmcvfs or xbmcplugin, which exist only
inside Kodi's own interpreter and cannot be installed alongside it.
"""
import json
import os
import socket
import ssl
import urllib.error
import urllib.request

#: Searched in order. LibreELEC and CoreELEC keep userdata under /storage.
DATA_DIRS = (
    "/storage/.kodi/userdata/addon_data/plugin.video.jellyfin",
    "~/.kodi/userdata/addon_data/plugin.video.jellyfin",
    "~/.var/app/tv.kodi.Kodi/data/userdata/addon_data/plugin.video.jellyfin",
    "~/Library/Application Support/Kodi/userdata/addon_data/plugin.video.jellyfin",
    "~/AppData/Roaming/Kodi/userdata/addon_data/plugin.video.jellyfin",
)

ADDON_ID = "plugin.video.jellyfin"
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


def load_json(path, default=None):
    try:
        with open(path, "rb") as handle:
            return json.load(handle)
    except (IOError, OSError, ValueError):
        if default is None:
            raise
        return default


def save_json(path, document):
    """Atomic, then owner only, matching what the add-on itself does."""
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


def auth_header(device_id, token=None):
    # Jellyfin refuses an unauthenticated request without this. The values
    # only name the session in the server's device list.
    header = 'MediaBrowser Client="%s", Device="%s", DeviceId="%s", Version="%s"' % (
        CLIENT,
        socket.gethostname(),
        device_id,
        VERSION,
    )
    if token:
        header += ', Token="%s"' % token
    return header


def jellyfin_request(server_url, path, device_id, payload=None, token=None,
                     insecure=False, method=None):
    url = server_url.rstrip("/") + "/" + path.lstrip("/")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None

    request = urllib.request.Request(
        url, data=body, method=method or ("POST" if body is not None else "GET")
    )
    request.add_header("Authorization", auth_header(device_id, token))
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json")

    context = None
    if insecure:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(request, timeout=15, context=context) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise SystemExit("%s %s: %s %s" % (request.get_method(), url, exc.code, detail))
    except urllib.error.URLError as exc:
        raise SystemExit("could not reach %s: %s" % (url, exc.reason))


def describe_server(server):
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
        print("  [%d] %s" % (index, describe_server(server)))
    try:
        return servers[int(input("server: ").strip())]
    except (ValueError, IndexError):
        raise SystemExit("not a listed server")
