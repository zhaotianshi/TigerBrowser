"""Configuration diagnostics remain safe on legacy consoles."""


def test_unknown_property_diagnostic_is_safe_on_legacy_consoles(monkeypatch):
    import io
    import sys

    from camoufox import utils

    output = io.BytesIO()
    console = io.TextIOWrapper(output, encoding='ascii', write_through=True)
    monkeypatch.setattr(sys, 'stdout', console)
    monkeypatch.setattr(utils, '_load_properties', lambda **kwargs: {})
    utils.validate_config({'unknown': ['Microphone®', '私人设备']})
    assert b'unknown' in output.getvalue()
    assert b'Microphone' not in output.getvalue()
