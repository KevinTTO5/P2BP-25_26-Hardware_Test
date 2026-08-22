"""Tests for mv3dt_installer.cameras (doc 00 §15).

Run from installer/: `python3 -m pytest tests/test_cameras.py -v`

No test opens a socket or spawns a real arp-scan/ffmpeg/ffprobe/ping --
every subprocess goes through an injected fake `runner`.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from mv3dt_installer import cameras  # noqa: E402

_SEED_PATH = pathlib.Path(__file__).resolve().parents[2] / "laptop" / "config" / "cameras.yml"


def _cp(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# normalize_mac / matches_oui
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["d0:3b:f4:01:52:79", "D0-3B-F4-01-52-79", "d03b.f401.5279", "D0:3b:F4:01:52:79"],
)
def test_normalize_mac_accepts_colon_dash_and_cisco_dot_forms(raw):
    assert cameras.normalize_mac(raw) == "d0:3b:f4:01:52:79"


def test_normalize_mac_rejects_garbage():
    with pytest.raises(ValueError):
        cameras.normalize_mac("not-a-mac")


def test_matches_oui_true_for_fleet_prefix():
    assert cameras.matches_oui("d0:3b:f4:01:52:79") is True


def test_matches_oui_false_for_other_vendor():
    assert cameras.matches_oui("aa:bb:cc:01:52:79") is False


def test_matches_oui_false_for_malformed_mac_not_an_error():
    assert cameras.matches_oui("garbage") is False


# ---------------------------------------------------------------------------
# parse_inventory / render_inventory
# ---------------------------------------------------------------------------


def test_render_then_parse_round_trips():
    original = [
        cameras.Camera(
            id="c1", mac="d0:3b:f4:01:52:79", ip="169.254.1.2", position="top-left"
        ),
        cameras.Camera(
            id="c2",
            mac="d0:3b:f4:01:52:91",
            ip="169.254.1.3",
            position="",
            enabled=False,
            stream_ok=True,
        ),
    ]

    text = cameras.render_inventory(original, header="# a header")
    parsed = cameras.parse_inventory(text)

    assert parsed == original


def test_parse_inventory_against_the_real_committed_seed_file():
    text = _SEED_PATH.read_text(encoding="utf-8")
    parsed = cameras.parse_inventory(text)

    assert len(parsed) == 8
    ids = [cam.id for cam in parsed]
    assert ids == [f"c{i}" for i in range(1, 9)]
    # The seed predates MAC tracking entirely -- doc 00 §15.1.
    assert all(cam.mac == "" for cam in parsed)
    assert parsed[0].ip == "169.254.9.14"
    assert parsed[0].position == "top-right"
    assert parsed[3].enabled is False  # c4
    assert all(cam.rtsp_path == "/Streaming/Channels/101" for cam in parsed)


def test_parse_inventory_on_malformed_text_returns_empty():
    assert cameras.parse_inventory("not yaml at all\njust text") == []


def test_parse_inventory_ignores_comments_and_blank_lines():
    text = (
        "# header comment\n"
        "\n"
        "cameras:\n"
        "  - id: c1\n"
        "    # a comment inside the block\n"
        "    ip: 1.2.3.4\n"
        '    position: "top-left"\n'
    )
    parsed = cameras.parse_inventory(text)
    assert len(parsed) == 1
    assert parsed[0].ip == "1.2.3.4"


# ---------------------------------------------------------------------------
# candidate_interfaces
# ---------------------------------------------------------------------------


def test_candidate_interfaces_drops_excluded_names_and_addressless_ifaces(tmp_path):
    for name in ("eth0", "lo", "docker0", "veth1234", "br-abc", "virbr0", "wlan0"):
        (tmp_path / name).mkdir()

    def runner(argv, **kwargs):
        if argv[:5] == ["ip", "-4", "-o", "addr", "show"]:
            if argv[-1] == "eth0":
                return _cp(argv, 0, stdout="1: eth0    inet 169.254.1.5/16 brd 169.254.255.255")
            return _cp(argv, 0, stdout="")
        return _cp(argv, 1)

    result = cameras.candidate_interfaces(runner=runner, net_dir=tmp_path)

    assert result == ["eth0"]


def test_candidate_interfaces_missing_net_dir_returns_empty(tmp_path):
    assert cameras.candidate_interfaces(net_dir=tmp_path / "does-not-exist") == []


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------

_ARP_SCAN_OUTPUT = (
    "Interface: eth0, type: EN10MB, MAC: 00:00:00:00:00:00, IPv4: 169.254.1.5\n"
    "Starting arp-scan\n"
    "169.254.1.10\td0:3b:f4:01:52:79\tUnknown\n"
    "169.254.1.11\taa:bb:cc:dd:ee:ff\tUnknown\n"
)


def test_discover_via_arp_scan_filters_by_oui_and_records_unmatched():
    def runner(argv, **kwargs):
        if argv[0] == "arp-scan":
            return _cp(argv, 0, stdout=_ARP_SCAN_OUTPUT)
        if argv[:5] == ["ip", "-4", "-o", "addr", "show"]:
            return _cp(argv, 0, stdout="inet 169.254.1.5/16")
        return _cp(argv, 1)

    result = cameras.discover(interfaces=["eth0"], runner=runner)

    assert result.tool == "arp-scan"
    assert len(result.cameras) == 1
    assert result.cameras[0].mac == "d0:3b:f4:01:52:79"
    assert result.cameras[0].ip == "169.254.1.10"
    assert result.unmatched == ["aa:bb:cc:dd:ee:ff"]


def test_discover_arp_scan_also_sweeps_cidr_when_interface_is_not_link_local():
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["arp-scan", "--interface"]:
            return _cp(argv, 0, stdout="")
        if argv[:5] == ["ip", "-4", "-o", "addr", "show"]:
            # A routable, non-link-local address.
            return _cp(argv, 0, stdout="inet 10.0.0.5/24")
        return _cp(argv, 1)

    cameras.discover(interfaces=["eth0"], cidr="10.0.0.0/24", runner=runner)

    arp_calls = [c for c in calls if c[0] == "arp-scan"]
    assert ["arp-scan", "--interface", "eth0", "--localnet"] in arp_calls
    assert ["arp-scan", "--interface", "eth0", "10.0.0.0/24"] in arp_calls


def test_discover_falls_back_to_ip_neigh_when_arp_scan_is_absent():
    def runner(argv, **kwargs):
        if argv[0] == "arp-scan":
            raise FileNotFoundError("arp-scan not found")
        if argv[0] == "ping":
            return _cp(argv, 0)
        if argv[:3] == ["ip", "-4", "neigh"]:
            return _cp(
                argv,
                0,
                stdout="169.254.1.10 dev eth0 lladdr d0:3b:f4:01:52:79 REACHABLE\n",
            )
        return _cp(argv, 1)

    result = cameras.discover(interfaces=["eth0"], prime_ips=["169.254.1.10"], runner=runner)

    assert result.tool == "ip-neigh"
    assert len(result.cameras) == 1
    assert result.cameras[0].mac == "d0:3b:f4:01:52:79"


def test_discover_ip_neigh_pings_every_prime_ip_first():
    pinged = []

    def runner(argv, **kwargs):
        if argv[0] == "arp-scan":
            raise FileNotFoundError()
        if argv[0] == "ping":
            pinged.append(argv[-1])
            return _cp(argv, 0)
        return _cp(argv, 0, stdout="")

    cameras.discover(prime_ips=["1.2.3.4", "1.2.3.5"], interfaces=["eth0"], runner=runner)

    assert pinged == ["1.2.3.4", "1.2.3.5"]


# ---------------------------------------------------------------------------
# probe_rtsp / grab_still
# ---------------------------------------------------------------------------


def test_probe_rtsp_true_on_success_and_password_never_reaches_a_log_call(capsys):
    cam = cameras.Camera(id="c1", mac="d0:3b:f4:01:52:79", ip="1.2.3.4", position="top-left")

    def runner(argv, **kwargs):
        assert "topsecret" in argv[-1]  # only ever in the argv, not logged
        return _cp(argv, 0)

    assert cameras.probe_rtsp(cam, user="admin", password="topsecret", runner=runner) is True
    assert "topsecret" not in capsys.readouterr().err


def test_probe_rtsp_false_on_failure():
    cam = cameras.Camera(id="c1", mac="d0:3b:f4:01:52:79", ip="1.2.3.4", position="top-left")
    assert (
        cameras.probe_rtsp(cam, user="a", password="b", runner=lambda argv, **kw: _cp(argv, 1))
        is False
    )


def test_grab_still_returns_path_when_ffmpeg_succeeds_and_file_exists(tmp_path):
    cam = cameras.Camera(id="c1", mac="d0:3b:f4:01:52:79", ip="1.2.3.4", position="top-left")
    dest = tmp_path / "cameras" / "still-d03bf4015279.jpg"

    def runner(argv, **kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake jpeg")
        return _cp(argv, 0)

    result = cameras.grab_still(cam, user="admin", password="pw", dest=dest, runner=runner)

    assert result == dest
    assert dest.is_file()


def test_grab_still_returns_none_when_ffmpeg_fails(tmp_path):
    cam = cameras.Camera(id="c1", mac="d0:3b:f4:01:52:79", ip="1.2.3.4", position="top-left")
    dest = tmp_path / "cameras" / "still.jpg"

    result = cameras.grab_still(
        cam, user="a", password="b", dest=dest, runner=lambda argv, **kw: _cp(argv, 1)
    )

    assert result is None


# ---------------------------------------------------------------------------
# bind_positions
# ---------------------------------------------------------------------------


def _cam(mac, position="", id=""):
    return cameras.Camera(id=id, mac=mac, ip="1.2.3.4", position=position)


def test_bind_positions_skips_cameras_that_already_have_a_position():
    cams = [_cam("d0:3b:f4:00:00:01", position="top-left", id="c1")]

    def _boom(prompt=""):
        raise AssertionError("must not prompt for an already-labeled camera")

    result = cameras.bind_positions(cams, non_interactive=False, prompt=_boom)

    assert result == cams


def test_bind_positions_non_interactive_assigns_mac_sorted_ids_and_leaves_position_blank():
    cams = [_cam("d0:3b:f4:00:00:02"), _cam("d0:3b:f4:00:00:01")]

    result = cameras.bind_positions(cams, non_interactive=True)

    by_mac = {cam.mac: cam for cam in result}
    assert by_mac["d0:3b:f4:00:00:01"].id == "c1"
    assert by_mac["d0:3b:f4:00:00:02"].id == "c2"
    assert all(cam.position == "" for cam in result)


def test_bind_positions_interactive_prompts_once_per_unlabeled_camera():
    cams = [_cam("d0:3b:f4:00:00:01"), _cam("d0:3b:f4:00:00:02")]
    answers = iter(["top-left", "bottom-right"])
    prompts = []

    def prompt(text):
        prompts.append(text)
        return next(answers)

    result = cameras.bind_positions(cams, non_interactive=False, prompt=prompt)

    assert len(prompts) == 2
    positions = {cam.mac: cam.position for cam in result}
    assert positions == {
        "d0:3b:f4:00:00:01": "top-left",
        "d0:3b:f4:00:00:02": "bottom-right",
    }


def test_bind_positions_interactive_logs_still_path_when_stills_dir_given(tmp_path, capsys):
    cams = [_cam("d0:3b:f4:00:00:01")]

    cameras.bind_positions(
        cams, non_interactive=False, prompt=lambda p: "top-left", stills_dir=tmp_path
    )

    assert "still-d03bf4000001.jpg" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


def test_merge_refreshes_ip_and_preserves_id_position_enabled():
    previous = [
        cameras.Camera(
            id="c1",
            mac="d0:3b:f4:00:00:01",
            ip="old-ip",
            position="top-left",
            enabled=False,
            stream_ok=True,
        )
    ]
    discovered = [cameras.Camera(id="", mac="d0:3b:f4:00:00:01", ip="new-ip", position="")]

    merged = cameras.merge(previous, discovered)

    assert len(merged) == 1
    assert merged[0].id == "c1"
    assert merged[0].ip == "new-ip"
    assert merged[0].position == "top-left"
    assert merged[0].enabled is False


def test_merge_retains_and_flags_a_camera_missing_from_this_scan():
    previous = [
        cameras.Camera(
            id="c1", mac="d0:3b:f4:00:00:01", ip="1.2.3.4", position="top-left", stream_ok=True
        )
    ]

    merged = cameras.merge(previous, [])

    assert len(merged) == 1
    assert merged[0].ip == "1.2.3.4"  # retained, not deleted
    assert merged[0].stream_ok is None  # flagged: not probed this scan


def test_merge_appends_a_newly_discovered_camera():
    previous = [cameras.Camera(id="c1", mac="d0:3b:f4:00:00:01", ip="1.2.3.4", position="top-left")]
    discovered = [cameras.Camera(id="", mac="d0:3b:f4:00:00:99", ip="9.9.9.9", position="")]

    merged = cameras.merge(previous, discovered)

    macs = {cam.mac for cam in merged}
    assert macs == {"d0:3b:f4:00:00:01", "d0:3b:f4:00:00:99"}


# ---------------------------------------------------------------------------
# refresh (end to end, fake runner throughout)
# ---------------------------------------------------------------------------


def test_refresh_first_run_writes_inventory_and_scan_json(tmp_path):
    install_dir = tmp_path / "install"

    def runner(argv, **kwargs):
        if argv[0] == "arp-scan":
            return _cp(argv, 0, stdout=_ARP_SCAN_OUTPUT)
        if argv[:5] == ["ip", "-4", "-o", "addr", "show"]:
            return _cp(argv, 0, stdout="inet 169.254.1.5/16")
        if argv[0] in ("ffprobe", "ffmpeg"):
            return _cp(argv, 0)
        return _cp(argv, 1)

    result = cameras.refresh(
        install_dir,
        seed_header="# seed header",
        cam_user="admin",
        cam_password="pw",
        interfaces=["eth0"],
        non_interactive=True,
        runner=runner,
    )

    assert len(result.cameras) == 1
    inventory_path = install_dir / "cameras.yml"
    scan_json_path = install_dir / "cameras.scan.json"
    assert inventory_path.is_file()
    assert scan_json_path.is_file()

    reparsed = cameras.parse_inventory(inventory_path.read_text(encoding="utf-8"))
    assert reparsed[0].mac == "d0:3b:f4:01:52:79"
    assert reparsed[0].id == "c1"  # non-interactive MAC-sorted assignment


def test_refresh_second_run_merges_with_the_previous_inventory(tmp_path):
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "cameras.yml").write_text(
        cameras.render_inventory(
            [
                cameras.Camera(
                    id="c1",
                    mac="d0:3b:f4:01:52:79",
                    ip="old-ip",
                    position="top-left",
                )
            ],
            header="# generated",
        ),
        encoding="utf-8",
    )

    def runner(argv, **kwargs):
        if argv[0] == "arp-scan":
            return _cp(argv, 0, stdout=_ARP_SCAN_OUTPUT)
        if argv[:5] == ["ip", "-4", "-o", "addr", "show"]:
            return _cp(argv, 0, stdout="inet 169.254.1.5/16")
        return _cp(argv, 1)

    result = cameras.refresh(install_dir, interfaces=["eth0"], non_interactive=True, runner=runner)

    assert len(result.cameras) == 1
    assert result.cameras[0].id == "c1"
    assert result.cameras[0].position == "top-left"
    assert result.cameras[0].ip == "169.254.1.10"  # refreshed


def test_refresh_missing_camera_is_retained_across_a_scan(tmp_path):
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "cameras.yml").write_text(
        cameras.render_inventory(
            [
                cameras.Camera(
                    id="c1", mac="d0:3b:f4:01:52:79", ip="169.254.1.10", position="top-left"
                )
            ],
            header="# generated",
        ),
        encoding="utf-8",
    )

    def runner(argv, **kwargs):
        # Nothing found this time -- the camera is powered off.
        if argv[0] == "arp-scan":
            return _cp(argv, 0, stdout="")
        if argv[:5] == ["ip", "-4", "-o", "addr", "show"]:
            return _cp(argv, 0, stdout="inet 169.254.1.5/16")
        return _cp(argv, 1)

    result = cameras.refresh(install_dir, interfaces=["eth0"], non_interactive=True, runner=runner)

    assert len(result.cameras) == 1
    assert result.cameras[0].id == "c1"
    assert result.cameras[0].ip == "169.254.1.10"
