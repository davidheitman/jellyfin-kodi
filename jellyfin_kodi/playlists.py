# -*- coding: utf-8 -*-

#################################################################################################

import os
import re
from urllib.parse import urlencode

import xbmcvfs

from . import downloader as server
from .helper import api, settings
from .jellyfin import Jellyfin
from .helper import LazyLogger

#################################################################################################

LOG = LazyLogger(__name__)

# Kodi keeps a folder per playlist type. Mixed playlists are the ones holding both
# audio and video, and they ARE listed by both the music and the video playlist
# windows: CUtil::MusicPlaylistsLocation() and CUtil::VideoPlaylistsLocation()
# each return a multipath of their own folder plus mixed/, and
# SpecialProtocol.cpp maps special://musicplaylists and special://videoplaylists
# straight to those. So mixed/ is the right home for a combined playlist and it
# is visible in the normal UI, not only over JSON-RPC.
#
# Playing one is the asymmetric part: VideoUtils::IsItemPlayable() claims
# mixed/, MusicUtils::IsItemPlayable() does not. Listed under both, played as
# video.
#
# SCOPE: the DEFAULT server only, deliberately. Jellyfin() is constructed
# without a server id, and the play urls below carry no `server` parameter, so
# the entrypoint's `server = params.get("server")` resolves None to that same
# default. Those two agree, which is why this works.
#
# That matches the rest of the add-on rather than getting ahead of it: library.py
# and views.py are single server too, and Jellyfin.get_active_clients() is called
# nowhere. Making playlists the one multi-server feature would put it out of step
# with everything it syncs alongside. Multi-server belongs in its own change,
# after or alongside making the rest of the sync multi-server, and needs a server
# id threaded through both the downloader calls and the play urls.
PLAYLIST_PATHS = {
    "music": "special://profile/playlists/music/",
    "video": "special://profile/playlists/video/",
    "mixed": "special://profile/playlists/mixed/",
}
# Jellyfin exposes a playlist entry's kind through MediaType. Anything else (photos,
# books) has no place in a Kodi playlist.
MEDIA_TYPES = {"Audio": "music", "Video": "video"}
# Same marker the view playlists and nodes use, so we only ever delete our own files.
PREFIX = "jellyfin_"
# Reserved on Windows, replaced rather than dropped so names stay recognizable.
ILLEGAL_CHARACTERS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
FILENAME_LIMIT = 100
# An m3u directive is a single line, a name holding a line break would split it.
LINE_BREAKS = re.compile(r"[\r\n]+")

#################################################################################################


class Playlists(object):
    """Mirror the server's playlists into Kodi's own playlist folders as m3u files.

    m3u rather than xsp: a smart playlist is a set of rules, it cannot express an
    arbitrary list of items in a given order, which is exactly what a playlist is.

    Kodi labels an m3u by its filename, so the playlist name has to be the filename.
    That makes the filename change whenever the playlist is renamed on the server,
    which is why every sync rewrites the full set and then drops any of our files
    that the current run did not write.
    """

    def __init__(self):

        self.server = Jellyfin()
        self.direct_path = settings("useDirectPaths") == "1"
        self.server_address = None

    def sync_playlists(self):
        """Entry point. Never raises, a failed playlist sync must not abort a library sync."""
        try:
            if not settings("syncPlaylists.bool"):
                self.delete_playlists()

                return

            if self.direct_path:
                self.server_address = self.server.auth.get_server_info(
                    self.server.auth.server_id
                )["address"]

            # Drain the listing before fetching any items. get_playlists() pages in
            # parallel, there is no reason to hold one of its threads while we walk
            # a playlist.
            playlists = []

            for batch in server.get_playlists():
                playlists.extend(batch["Items"])

            written = set()

            for playlist in playlists:

                path = self.add_playlist(playlist, written)

                if path:
                    written.add(path)

            self.delete_playlists(keep=written)
            LOG.info("--[ playlists/%s ]", len(written))
        except Exception as error:
            LOG.exception(error)

    def add_playlist(self, playlist, written):
        """Write a single playlist. Returns the path we wrote, relative to the profile."""
        items = []

        for item in server.get_playlist_items(
            playlist["Id"], api.info() if self.direct_path else None
        ):
            if item.get("MediaType") in MEDIA_TYPES:
                items.append(item)
            else:
                LOG.debug(
                    "[ skip playlist entry/%s ] %s", item.get("Id"), item.get("Name")
                )

        media = self.get_media_type(playlist, items)
        path = self.get_playlist_path(playlist, media, written)

        self.write_playlist(path, self.build_playlist(playlist, items))

        return path

    def get_media_type(self, playlist, items):
        """music, video or mixed, decided by what the playlist actually holds.

        The playlist's own MediaType only describes what it was created as. A
        playlist created for music can still have had a movie dropped into it.
        """
        media = set(MEDIA_TYPES[x["MediaType"]] for x in items)

        if len(media) == 1:
            return media.pop()

        if media:
            return "mixed"

        # Nothing playable in it, fall back to how the server labels the playlist.
        return MEDIA_TYPES.get(playlist.get("MediaType"), "video")

    def get_playlist_path(self, playlist, media, written):
        """Two playlists can share a name, the filename cannot. Fall back to the id."""
        path = "%s/%s%s.m3u" % (media, PREFIX, self.normalize_name(playlist["Name"]))

        if path in written:
            path = "%s/%s%s (%s).m3u" % (
                media,
                PREFIX,
                self.normalize_name(playlist["Name"]),
                playlist["Id"][:8],
            )

        return path

    def normalize_name(self, name):

        name = ILLEGAL_CHARACTERS.sub("_", name)
        name = name.strip().rstrip(".").strip()

        return name[:FILENAME_LIMIT] or "playlist"

    def build_playlist(self, playlist, items):
        """Render the extended m3u."""
        lines = ["#EXTM3U", "#PLAYLIST:%s" % self.one_line(playlist["Name"])]
        resolved = 0

        for item in items:

            path = self.get_item_path(item)

            if not path:
                LOG.info(
                    "[ unresolved playlist entry/%s ] %s", item["Id"], item.get("Name")
                )

                continue

            lines.append(
                "#EXTINF:%s,%s" % (self.get_runtime(item), self.get_label(item))
            )
            lines.append(path)
            resolved += 1

        if items and not resolved:
            LOG.info("[ empty playlist/%s ] %s", playlist["Id"], playlist["Name"])

        return "\n".join(lines) + "\n"

    def get_item_path(self, item):
        """The path Kodi should play.

        In add-on mode this is the same play url the add-on's own browse listing
        hands Kodi, so playback, transcoding and playstate reporting behave as they
        do everywhere else in the add-on, and entries that were never synced into
        the Kodi library still resolve. In direct path mode we point at the file.
        """
        if self.direct_path:
            return api.API(item, self.server_address).get_file_path(item.get("Path"))

        return "%s?%s" % (
            "plugin://plugin.video.jellyfin/",
            urlencode({"id": item["Id"], "mode": "play"}),
        )

    def get_runtime(self, item):

        ticks = item.get("RunTimeTicks")

        return int(ticks / 10000000) if ticks else -1

    def one_line(self, text):
        return LINE_BREAKS.sub(" ", text).strip()

    def get_label(self, item):

        name = self.one_line(item.get("Name") or "")

        if item.get("Type") == "Episode" and item.get("SeriesName"):
            return "%s - %s" % (item["SeriesName"], name)

        artists = item.get("Artists") or []

        if artists:
            return "%s - %s" % (", ".join(artists), name)

        return name

    def translate(self, path):
        """Profile relative path, i.e. music/jellyfin_Road trip.m3u, to a real path."""
        media, filename = path.split("/", 1)

        return os.path.join(xbmcvfs.translatePath(PLAYLIST_PATHS[media]), filename)

    def write_playlist(self, path, content):
        """Only touch the file when it changed, so Kodi is not told to reload for nothing."""
        file = self.translate(path)
        folder = os.path.dirname(file)

        os.makedirs(folder, exist_ok=True)

        try:
            with open(file, "r", encoding="utf-8") as infile:
                if infile.read() == content:
                    return
        except Exception:
            pass

        with open(file, "w", encoding="utf-8", newline="\n") as outfile:
            outfile.write(content)

        LOG.info("UPDATE playlist %s", path)

    def delete_playlist(self, path):

        xbmcvfs.delete(self.translate(path))
        LOG.info("DELETE playlist %s", path)

    def delete_playlists(self, keep=None):
        """Remove our m3u playlists, except the ones the caller just wrote."""
        keep = keep or set()

        for media in PLAYLIST_PATHS:

            _, files = xbmcvfs.listdir(xbmcvfs.translatePath(PLAYLIST_PATHS[media]))

            for file in files:

                path = "%s/%s" % (media, file)

                if (
                    file.startswith(PREFIX)
                    and file.endswith(".m3u")
                    and path not in keep
                ):
                    self.delete_playlist(path)
