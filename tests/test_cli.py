from pymooserver.cli import build_unified_parser


def test_default_cli_configuration():
    args = build_unified_parser().parse_args([])
    assert args.host == "0.0.0.0"
    assert args.moo12_ports is None
    assert args.mooapi_ports is None
    assert args.mooapi_dialect == "auto"
    assert args.no_moodplay is False


def test_moodplay_cli_can_be_disabled():
    args = build_unified_parser().parse_args(["--no-moodplay"])
    assert args.no_moodplay is True
