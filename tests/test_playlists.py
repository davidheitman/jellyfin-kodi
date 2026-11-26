# -*- coding: utf-8 -*-

import pytest

from jellyfin_kodi.playlists import Playlists


def make_playlists():
    """Playlists without __init__, which would need a server and the add-on settings."""
    playlists = Playlists.__new__(Playlists)
    playlists.direct_path = False
    playlists.server_address = "http://localhost:8096"

    return playlists


def song(item_id, name, artists=None, ticks=1200000000):
    return {
        "Id": item_id,
        "Name": name,
        "Type": "Audio",
        "MediaType": "Audio",
        "Artists": artists or [],
        "RunTimeTicks": ticks,
    }


def movie(item_id, name, ticks=72000000000):
    return {
        "Id": item_id,
        "Name": name,
        "Type": "Movie",
        "MediaType": "Video",
        "RunTimeTicks": ticks,
    }


@pytest.mark.parametrize(
    "playlist,items,expected",
    [
        ({"MediaType": "Audio"}, [song("1", "a"), song("2", "b")], "music"),
        ({"MediaType": "Video"}, [movie("1", "a")], "video"),
        ({"MediaType": "Audio"}, [song("1", "a"), movie("2", "b")], "mixed"),
        # Empty playlists fall back to how the server labels them.
        ({"MediaType": "Audio"}, [], "music"),
        ({"MediaType": "Video"}, [], "video"),
        ({}, [], "video"),
    ],
)
def test_get_media_type(playlist, items, expected):
    assert make_playlists().get_media_type(playlist, items) == expected


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Road trip", "Road trip"),
        ("AC/DC", "AC_DC"),
        ('why: "this"?', "why_ _this__"),
        ("trailing dots...", "trailing dots"),
        ("  padded  ", "padded"),
        ("...", "playlist"),
        ("Ambiance été", "Ambiance été"),
    ],
)
def test_normalize_name(name, expected):
    assert make_playlists().normalize_name(name) == expected


def test_normalize_name_is_length_capped():
    assert len(make_playlists().normalize_name("x" * 500)) == 100


def test_get_playlist_path():
    playlists = make_playlists()
    first = playlists.get_playlist_path({"Id": "aaaa", "Name": "Mix"}, "music", set())

    assert first == "music/jellyfin_Mix.m3u"

    # A second playlist of the same name must not overwrite the first.
    second = playlists.get_playlist_path(
        {"Id": "bbbbccccdddd", "Name": "Mix"}, "music", {first}
    )

    assert second == "music/jellyfin_Mix (bbbbcccc).m3u"


def test_build_playlist_preserves_order_and_uses_plugin_paths():
    playlists = make_playlists()
    items = [song("s1", "First", ["Band"]), song("s2", "Second")]

    assert playlists.build_playlist({"Id": "p", "Name": "Mix"}, items) == (
        "#EXTM3U\n"
        "#PLAYLIST:Mix\n"
        "#EXTINF:120,Band - First\n"
        "plugin://plugin.video.jellyfin/?id=s1&mode=play\n"
        "#EXTINF:120,Second\n"
        "plugin://plugin.video.jellyfin/?id=s2&mode=play\n"
    )


def test_build_playlist_empty():
    assert make_playlists().build_playlist({"Id": "p", "Name": "Mix"}, []) == (
        "#EXTM3U\n#PLAYLIST:Mix\n"
    )


def test_get_label_episode():
    episode = {
        "Id": "e1",
        "Name": "Pilot",
        "Type": "Episode",
        "MediaType": "Video",
        "SeriesName": "Show",
    }

    assert make_playlists().get_label(episode) == "Show - Pilot"


def test_get_runtime_without_duration():
    assert make_playlists().get_runtime({"Id": "1"}) == -1


def test_delete_playlists_only_touches_our_own_files(monkeypatch):
    """The one piece of this that removes user files, so be explicit about the rule."""
    import jellyfin_kodi.playlists as module

    listings = {
        "music": ["jellyfin_Road trip.m3u", "my own mixtape.m3u", "jellyfin_notes.txt"],
        "video": ["jellyfin_Shorts.m3u", "jellyfin_view_abc.xsp"],
        "mixed": ["jellyfin_Everything.m3u"],
    }

    def fake_listdir(path):
        for media in listings:
            if media in path:
                return [], listings[media]

        return [], []

    deleted = []
    monkeypatch.setattr(module.xbmcvfs, "listdir", fake_listdir)
    monkeypatch.setattr(module.xbmcvfs, "translatePath", lambda p: p)
    monkeypatch.setattr(module.xbmcvfs, "delete", lambda p: deleted.append(p))

    playlists = make_playlists()
    playlists.delete_playlists(keep={"music/jellyfin_Road trip.m3u"})

    assert deleted == [
        "special://profile/playlists/video/jellyfin_Shorts.m3u",
        "special://profile/playlists/mixed/jellyfin_Everything.m3u",
    ]
