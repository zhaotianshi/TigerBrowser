"""Timezone choices follow the active database metadata and its canonical records."""

import zoneinfo

import pytest

from camoufox.studio.timezones import timezone_catalog


def test_catalog_reads_the_active_metadata_without_link_aliases(tmp_path, monkeypatch):
    (tmp_path / 'tzdata.zi').write_text(
        '# version 2026e\nZ Asia/Shanghai 8 - CST\nZ Etc/UTC 0 - UTC\n'
        'L America/Los_Angeles US/Pacific\nZ Factory 0 - -00\n', encoding='utf-8')
    monkeypatch.setattr(zoneinfo, 'TZPATH', (str(tmp_path),))
    assert timezone_catalog() == {'version': '2026e', 'timezones': [
        {'name': 'Asia/Shanghai', 'region': 'Asia'}, {'name': 'Etc/UTC', 'region': 'Etc'}]}


def test_catalog_fails_for_partial_system_data_instead_of_using_other_metadata(tmp_path, monkeypatch):
    (tmp_path / 'custom-data').write_bytes(b'TZif')
    monkeypatch.setattr(zoneinfo, 'TZPATH', (str(tmp_path),))
    with pytest.raises(FileNotFoundError):
        timezone_catalog()


def test_catalog_fails_when_metadata_names_do_not_validate(tmp_path, monkeypatch):
    (tmp_path / 'tzdata.zi').write_text('# version test\nZ Invalid/Timezone 0 - TEST\n', encoding='utf-8')
    monkeypatch.setattr(zoneinfo, 'TZPATH', (str(tmp_path),))
    with pytest.raises(zoneinfo.ZoneInfoNotFoundError):
        timezone_catalog()
