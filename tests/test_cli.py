from wearable_logs_ocr.cli import _geometry_tokens, build_parser


def test_geometry_accepts_quoted_comma_form() -> None:
    arguments = build_parser().parse_args(["capture", "--crop", "0.1,0.2,0.9,0.8"])
    assert _geometry_tokens(arguments.crop, "crop") == [0.1, 0.2, 0.9, 0.8]


def test_geometry_accepts_powershell_expanded_form() -> None:
    arguments = build_parser().parse_args(["capture", "--crop", "0.1", "0.2", "0.9", "0.8"])
    assert _geometry_tokens(arguments.crop, "crop") == [0.1, 0.2, 0.9, 0.8]
