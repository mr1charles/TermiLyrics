import asyncio

from lyrics_sync.config import AppConfig, Paths
from lyrics_sync.demo import load_demo
from lyrics_sync.keyboard import parse_keys
from lyrics_sync.main import LyricsApp, build_parser
from lyrics_sync.player import _SEP, PlayerSource, parse_all_players
from lyrics_sync.settings import AdjustmentStore
from lyrics_sync.terminal import COLOR_16, COLOR_256, COLOR_NONE, COLOR_TRUE, detect_color_mode, fg_params


def test_parse_keys():
    assert parse_keys("a\x1b[D\x1b[C\t\x1b[Z\x7f \x1b") == ["a", "left", "right", "tab", "shift-tab",
                                                          "backspace", "space", "esc"]
    assert parse_keys("\x1b[15~q") == ["q"]  # unknown sequence (F5) is skipped whole


def test_parse_all_players_and_ranking():
    row = lambda *f: _SEP.join(f)
    out = "\n".join([
        row("spotify", "spotify", "Paused", "12000000", "200000000", "Queen", "Bohemian Rhapsody", "", ""),
        row("firefox.instance_1", "firefox", "Playing", "5500000", "", "Artist", "Song", "", "https://x"),
        "No player could handle this command",
    ])
    states = parse_all_players(out, 1.0)
    assert [s.player for s in states] == ["spotify", "firefox.instance_1"]
    assert states[0].position == 12.0 and states[0].length == 200.0 and states[1].length is None
    src = PlayerSource()
    assert min(states, key=src._rank).player == "firefox.instance_1"  # playing beats preferred-but-paused


def test_color_detection():
    assert detect_color_mode({"COLORTERM": "truecolor"}) == COLOR_TRUE
    assert detect_color_mode({"TERM": "xterm-256color"}) == COLOR_256
    assert detect_color_mode({"TERM": "linux"}) == COLOR_16
    assert detect_color_mode({"NO_COLOR": "1", "COLORTERM": "truecolor"}) == COLOR_NONE
    assert detect_color_mode({"TERMILYRICS_COLOR": "256", "COLORTERM": "truecolor"}) == COLOR_256
    assert fg_params((255, 0, 0), COLOR_256) == "38;5;196"
    assert fg_params((255, 0, 0), COLOR_NONE) == ""


def test_cli_parsing():
    p = build_parser()
    a = p.parse_args(["--mode", "sing-along", "--theme", "dracula", "-e", "drop", "--demo"])
    assert (a.mode, a.theme, a.animation, a.demo) == ("karaoke", "dracula", "drop", "")
    assert p.parse_args(["2"]).legacy_mode == "2"
    assert p.parse_args(["-m", "6"]).mode == "hacker_matrix"


def test_app_loads_demo_and_retimes_slowed_track(tmp_path):
    config = AppConfig(paths=Paths(home=tmp_path))
    player, source = load_demo(None, speed=0.8)
    app = LyricsApp(config, player=player, lyrics_source=source)

    async def scenario():
        state = player.read()
        from lyrics_sync.detect import identify
        song = identify(state.title, state.artist)
        app.latest_state = state
        app.player_missing = False
        await app._start_song(song, state, force_retry=False)
        await app._preload_task
        return song

    song = asyncio.run(scenario())
    assert song.variant == "slowed"
    assert app.timeline is not None and app.lyrics.has_word_timing
    assert abs(app.tempo_map().scale - 0.8) < 0.01 and app.tempo_estimate.source == "durations"
    scene = app.build_scene(0.0)
    assert scene.timeline is app.timeline and scene.status.variant == "slowed"

    # Sync keys adjust and persist per track
    app._handle_key("left")
    app._handle_key("+")
    assert app.adjustment.offset == 0.1 and app.adjustment.scale == 0.81
    stored = AdjustmentStore(config.paths.adjustments_file).get(app._adjust_key)
    assert stored.offset == 0.1 and stored.scale == 0.81
    app._handle_key("0")
    assert app.adjustment.is_default

    # Display keys change and persist settings
    app._handle_key("2")
    assert app.live.display_mode == "karaoke"
    assert app.settings_store.load()["display_mode"] == "karaoke"
    app._handle_key("q")
    assert not app._running
