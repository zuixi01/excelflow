import pytest

from excel_auditor.workflow.excel import validate_records
from excel_auditor.workflow.models import WorkflowConfig


def config(name='warranty_days', kind='decimal', **rule_options):
    return WorkflowConfig(name='Warranty', fields=[{'rule': {'name': name, 'title': '质保天数', 'type': kind, **rule_options}}],
                          outputs=[{'name': 'result.xlsx', 'sheets': [{'columns': [{'field': name, 'title': '质保天数'}]}]}])


@pytest.mark.parametrize('kind', ['integer', 'decimal'])
@pytest.mark.parametrize('value,expected', [('5840天', '5840'), (' 5840 天 ', '5840'), ('５８４０天', '5840'),
                                           ('0天', '0'), ('5840', '5840'), ('', '')])
def test_warranty_day_suffix_normalizes_without_overwriting_source(kind, value, expected):
    rows = [{'values_json': {'warranty_days': value}, 'original': {'warranty_days': value}}]
    assert validate_records(rows, config(kind=kind)) == (0, 0)
    assert rows[0]['values_json']['warranty_days'] == expected
    assert rows[0]['original']['warranty_days'] == value
    assert validate_records(rows, config(kind=kind)) == (0, 0)


@pytest.mark.parametrize('value', ['1年', '12个月', '5-10天', '十天', '5840天保修', '-5天', '5.5天', '=5840天'])
def test_ambiguous_warranty_durations_still_require_correction(value):
    rows = [{'values_json': {'warranty_days': value}}]
    assert validate_records(rows, config())[0] == 1
    assert rows[0]['values_json']['warranty_days'] == value


def test_day_suffix_is_field_specific_and_respects_validation_limits():
    rows = [{'values_json': {'sale_price': '5840天'}}]
    assert validate_records(rows, config(name='sale_price'))[0] == 1
    assert rows[0]['values_json']['sale_price'] == '5840天'
    rows = [{'values_json': {'warranty_days': '5840天'}}]
    assert validate_records(rows, config(kind='string')) == (0, 0)
    assert rows[0]['values_json']['warranty_days'] == '5840天'
    assert validate_records(rows, config(validation={'max': '3650'}))[0] == 1
    assert rows[0]['values_json']['warranty_days'] == '5840天'
