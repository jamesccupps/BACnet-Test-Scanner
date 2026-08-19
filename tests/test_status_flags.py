"""Tests for the status-flags decoder and the small pure helpers around it.

_format_status_flags read the flag bits by attribute name. bacpypes3 exposes
`StatusFlags.fault` and `.overridden` as class-level BIT POSITION constants
(1 and 2), not as this instance's value for that bit, so `getattr(sf, "fault")`
returned a truthy 1 on every object. `in_alarm` and `out_of_service` are not
exposed under those names, so those two fell through to positional indexing and
happened to be correct — the fallback path worked and the primary one did not.

The result was FAULT and OVR reported on every single point, which turned every
row pink and made the fault count equal the object count. The one thing this
tool exists to do — show which points are faulted — was the thing it could not
do.

These tests pin the decoder against real bacpypes3 StatusFlags values rather
than hand-made stand-ins, because the bug was in how the real type behaves
under attribute access.
"""

import pytest

import p2_bridge_scanner as scanner

StatusFlags = pytest.importorskip("bacpypes3.basetypes").StatusFlags


# -- the four flags, individually -------------------------------------------

@pytest.mark.parametrize("bits,expected", [
    ([0, 0, 0, 0], "0000"),
    ([1, 0, 0, 0], "1000 ALARM"),
    ([0, 1, 0, 0], "0100 FAULT"),
    ([0, 0, 1, 0], "0010 OVR"),
    ([0, 0, 0, 1], "0001 OOS"),
])
def test_each_flag_decodes_on_its_own(bits, expected):
    assert scanner._format_status_flags(StatusFlags(bits)) == expected


@pytest.mark.parametrize("bits,expected", [
    ([1, 1, 0, 0], "1100 ALARM,FAULT"),
    ([0, 1, 0, 1], "0101 FAULT,OOS"),
    ([1, 1, 1, 1], "1111 ALARM,FAULT,OVR,OOS"),
])
def test_combinations(bits, expected):
    assert scanner._format_status_flags(StatusFlags(bits)) == expected


def test_healthy_point_reports_nothing():
    """The regression. A point with no flags set must not read as faulted:
    every row used to come back FAULT,OVR, so nothing stood out."""
    out = scanner._format_status_flags(StatusFlags([0, 0, 0, 0]))
    assert "FAULT" not in out
    assert "OVR" not in out
    assert out == "0000"


def test_fault_is_reported_only_when_actually_set():
    assert "FAULT" not in scanner._format_status_flags(StatusFlags([1, 0, 1, 1]))
    assert "FAULT" in scanner._format_status_flags(StatusFlags([0, 1, 0, 0]))


def test_overridden_is_reported_only_when_actually_set():
    assert "OVR" not in scanner._format_status_flags(StatusFlags([1, 1, 0, 1]))
    assert "OVR" in scanner._format_status_flags(StatusFlags([0, 0, 1, 0]))


def test_bit_order_matches_ashrae_135():
    """in-alarm, fault, overridden, out-of-service. Getting this order wrong
    would mislabel every point rather than fail visibly."""
    for i, label in enumerate(("ALARM", "FAULT", "OVR", "OOS")):
        bits = [0, 0, 0, 0]
        bits[i] = 1
        out = scanner._format_status_flags(StatusFlags(bits))
        assert out.split()[1] == label
        assert out.split()[0][i] == "1"


# -- values that are not a bit string ---------------------------------------

class TestNonBitStringInput:
    """Unknown must not render as "everything is in alarm". The old sentinel
    was the string "?", which is truthy, so a missing or errored status-flags
    came out as 1111 ALARM,FAULT,OVR,OOS — maximum alarm from no data."""

    @pytest.mark.parametrize("value", [
        None,
        "communicationFailure",     # an error string is subscriptable
        "",
        b"abcd",
        bytearray(b"abcd"),
        0,
        1,
        3.5,
        object(),
        [],
        [1, 0],                     # too short
        ["a", "b", "c", "d"],       # right length, wrong contents
        {"fault": 1},
    ])
    def test_returns_empty_rather_than_a_false_alarm(self, value):
        out = scanner._format_status_flags(value)
        assert out == "", f"{value!r} produced {out!r}"
        assert "FAULT" not in out
        assert "ALARM" not in out

    def test_an_error_string_does_not_index_character_by_character(self):
        """'communicationFailure'[0:4] are all truthy characters, so a naive
        implementation reports all four flags set."""
        assert scanner._format_status_flags("communicationFailure") == ""


# -- the GUI keys its highlighting off this string --------------------------

class TestDownstreamHighlighting:
    """_refresh_rows_view tags any row whose status_flags contains FAULT, and
    the summary line counts the same way. Both depend on this decoder being
    right, which is why the bug showed up as every row pink."""

    def test_healthy_rows_do_not_contain_the_fault_marker(self):
        assert "FAULT" not in scanner._format_status_flags(StatusFlags([0, 0, 0, 0]))

    def test_a_run_of_healthy_points_counts_zero_faults(self):
        flags = [scanner._format_status_flags(StatusFlags([0, 0, 0, 0]))
                 for _ in range(50)]
        assert sum(1 for f in flags if "FAULT" in f) == 0

    def test_only_the_genuinely_faulted_point_is_counted(self):
        healthy = [StatusFlags([0, 0, 0, 0])] * 9
        faulted = [StatusFlags([0, 1, 0, 0])]
        flags = [scanner._format_status_flags(s) for s in healthy + faulted]
        assert sum(1 for f in flags if "FAULT" in f) == 1


# -- neighbouring helpers ---------------------------------------------------

def test_short_type_strips_nothing_it_should_not():
    assert scanner._short_type("analogInput") == "analogInput"


def test_count_by_type_on_empty_input():
    assert scanner._count_by_type([]) == {}


def test_format_pv_of_none_is_blank_not_the_word_none():
    assert scanner._format_pv(None) == ""


def test_detect_local_ip_returns_something_ip_shaped():
    ip = scanner._detect_local_ip()
    parts = ip.split(".")
    assert len(parts) == 4
    assert all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)
