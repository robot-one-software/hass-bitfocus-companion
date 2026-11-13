"""Wire-format parsing and formatting."""

from custom_components.bitfocus_companion.satellite import (
    Location,
    decode_text,
    format_message,
    parse_line,
    parse_version,
    quote_value,
    truthy,
)


def test_parse_begin_with_quoted_values() -> None:
    command, status, params = parse_line('BEGIN CompanionVersion="5.1.1+10000-stable-1e2454ae78" ApiVersion="1.14.0" ')
    assert command == "BEGIN"
    assert status is None
    assert params == {"CompanionVersion": "5.1.1+10000-stable-1e2454ae78", "ApiVersion": "1.14.0"}


def test_parse_caps_and_status_lines() -> None:
    command, status, params = parse_line('CAPS SUBSCRIPTIONS=0 NONSQUARE=1 BITMAP_FORMATS="rgb,png,webp"')
    assert command == "CAPS"
    assert params["SUBSCRIPTIONS"] == "0"
    assert params["BITMAP_FORMATS"] == "rgb,png,webp"

    command, status, params = parse_line('ADD-SUB ERROR MESSAGE="Subscriptions not enabled"')
    assert (command, status) == ("ADD-SUB", "ERROR")
    assert params["MESSAGE"] == "Subscriptions not enabled"

    command, status, params = parse_line("SUB-PRESS OK SUBID=ha-1-0-0")
    assert (command, status, params) == ("SUB-PRESS", "OK", {"SUBID": "ha-1-0-0"})


def test_parse_escapes_and_bare_flags() -> None:
    _, _, params = parse_line('X A="say \\"hi\\" there" B=1 FLAG')
    assert params == {"A": 'say "hi" there', "B": "1", "FLAG": True}


def test_format_message_quotes_like_companion() -> None:
    assert format_message("ADD-SUB", SUBID="ha-1-0-0", LOCATION="1/0/0", TEXT=True, COLORS="hex") == (
        "ADD-SUB SUBID=ha-1-0-0 LOCATION=1/0/0 TEXT=1 COLORS=hex\n"
    )
    assert format_message("SUB-ROTATE", SUBID="x", DIRECTION=-3) == "SUB-ROTATE SUBID=x DIRECTION=-3\n"
    assert quote_value("with space") == '"with space"'
    assert quote_value('q"uote') == '"q\\"uote"'
    assert quote_value("") == '""'


def test_helpers() -> None:
    assert truthy("1") and truthy("true") and truthy(True)
    assert not truthy("0") and not truthy("false") and not truthy(None) and not truthy("")
    assert decode_text("Q2FtIDE=") == "Cam 1"
    assert decode_text(None) is None
    assert parse_version("1.14.0") == (1, 14, 0)
    assert parse_version("1.10.0") >= (1, 10, 0)
    assert parse_version("1.9.1") < (1, 10, 0)
    location = Location(2, 1, 3)
    assert location.sub_id == "ha-2-1-3"
    assert str(location) == "2/1/3"
    assert Location.from_sub_id("ha-2-1-3") == location
    assert Location.from_sub_id("other") is None
