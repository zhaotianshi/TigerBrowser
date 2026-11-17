"""Interrupted writes and Unicode paths cannot produce a silently wrong font environment."""

from pathlib import Path
from xml.etree import ElementTree

import pytest

from camoufox import utils


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    root = tmp_path / '研究&字体'
    configuration = root / 'fontconfig/linux'
    configuration.mkdir(parents=True)
    (root / 'fonts/linux').mkdir(parents=True)
    (configuration / 'fonts.conf').write_text(
        '<?xml version="1.0"?><fontconfig><dir prefix="cwd">fonts</dir></fontconfig>', encoding='utf-8')
    monkeypatch.setattr(utils, 'INSTALL_DIR', tmp_path / 'cache')
    return root, configuration


def test_fontconfig_escapes_unicode_bundle_paths(bundle):
    root, configuration = bundle
    result = utils._generate_fontconfig(str(configuration), path=root / 'camoufox', os_dir='linux')
    assert ElementTree.parse(result).find('dir').text == str(root / 'fonts/linux')


@pytest.mark.parametrize('corruption', [b'', b'<fontconfig>', b'wrong data'])
def test_fontconfig_repairs_corrupt_derived_cache_not_bundle(bundle, corruption):
    root, configuration = bundle
    result = Path(utils._generate_fontconfig(str(configuration), path=root / 'camoufox', os_dir='linux'))
    expected = result.read_bytes()
    result.write_bytes(corruption)
    assert utils._generate_fontconfig(str(configuration), path=root / 'camoufox', os_dir='linux') == str(result)
    assert result.read_bytes() == expected
    assert 'prefix="cwd"' in (configuration / 'fonts.conf').read_text(encoding='utf-8')
