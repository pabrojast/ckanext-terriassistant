import math

import pandas as pd
import pytest

from ckanext.terriassistant.data_assistant.calculations import calculate, compare, profile
from ckanext.terriassistant.data_assistant.tools import validate_call


def test_statistics_exclude_missing_and_infinite_not_zero():
    result = calculate(pd.DataFrame({'v': [0, '2', 4, None, '', math.inf]}), {'operation': 'statistics', 'column': 'v'})
    s = result['statistics']
    assert s['count'] == 3 and s['missing'] == 3
    assert s['mean'] == 2 and s['sum'] == 6 and s['median'] == 2
    assert s['stddev'] == pytest.approx(math.sqrt(8 / 3))


def test_empty_is_not_zero():
    s = calculate(pd.DataFrame({'v': [None]}), {'operation': 'statistics', 'column': 'v'})['statistics']
    assert s['count'] == 0 and s['mean'] is None and s['sum'] is None


def test_mixed_boolean_cells_are_not_numeric_observations():
    s = calculate(pd.DataFrame({'v': [True, False, 1, '3', None]}), {'operation': 'statistics', 'column': 'v'})['statistics']
    assert s['count'] == 2 and s['mean'] == 2 and s['missing'] == 3


def test_full_dataset_not_profile_head():
    frame = pd.DataFrame({'v': [1] * 5000 + [10001]})
    assert profile(frame)['sample_only'] is True
    assert calculate(frame, {'operation': 'statistics', 'column': 'v'})['statistics']['mean'] == pytest.approx(15001 / 5001)


def test_filter_numeric_order_pagination():
    frame = pd.DataFrame({'v': ['2', '10', '1'], 'site': ['A', 'A', 'B']})
    r = calculate(frame, {'operation': 'rows', 'filters': [{'column': 'site', 'op': 'eq', 'value': 'A'}], 'order_by': 'v', 'descending': True, 'limit': 1})
    assert r['rows'] == [{'v': '10', 'site': 'A'}] and r['truncated']
    assert r['rows_used'] == 2 and r['rows_total'] == 3


def test_monthly_aggregation_and_inclusive_end():
    frame = pd.DataFrame({'date': ['2024-01-01', '2024-01-31T23:59:00Z', '2024-02-01', 'bad'], 'v': [1, 3, 9, 10]})
    r = calculate(frame, {'operation': 'time_series', 'date_column': 'date', 'column': 'v', 'end': '2024-01-31'})
    assert r['rows'] == [{'x': '2024-01', 'y': 2.0, 'count': 2}]


def test_group_all_null_sum_stays_missing():
    r = calculate(pd.DataFrame({'group': ['A', 'B'], 'v': [None, 1]}), {'operation': 'group', 'group_by': 'group', 'column': 'v', 'metric': 'sum'})
    assert next(row['y'] for row in r['rows'] if row['x'] == 'A') is None


def test_trend_and_short_period():
    f = pd.DataFrame({'date': ['2020-01-01', '2021-01-01', '2022-01-01'], 'v': [0, 366, 731]})
    r = calculate(f, {'operation': 'trend', 'column': 'v', 'date_column': 'date'})
    assert r['statistics']['slope_per_year'] == pytest.approx(365.25)
    assert r['statistics']['r_squared'] == pytest.approx(1)
    with pytest.raises(ValueError):
        calculate(pd.DataFrame({'date': ['2024-01-01', '2024-01-02', '2024-01-03'], 'v': [1, 2, 3]}), {'operation': 'trend', 'column': 'v', 'date_column': 'date'})


def test_correlation_aligns_exact_keys_and_rejects_duplicate_periods():
    a = {'rows': [{'x': str(x), 'y': x} for x in range(5)]}
    b = {'rows': [{'x': str(x), 'y': 2 * x} for x in range(1, 6)]}
    assert compare(a, b, 'correlation')['statistics']['count'] == 4
    assert compare(a, b, 'correlation')['statistics']['pearson_r'] == pytest.approx(1)
    b['rows'].append(b['rows'][0])
    with pytest.raises(ValueError):
        compare(a, b, 'correlation')


def test_zero_baseline_and_incompatible_units():
    a, b = {'statistics': {'mean': 0}, 'units': 'mg/L'}, {'statistics': {'mean': 2}, 'units': 'mg/L'}
    assert compare(a, b, 'difference')['statistics'] == {'left_mean': 0, 'right_mean': 2, 'difference': 2, 'percent_change': None}
    b['units'] = 'degrees'
    with pytest.raises(ValueError):
        compare(a, b, 'difference')


def test_outliers_and_polygon_hole():
    f = pd.DataFrame({'v': [1, 2, 2, 3, 100]})
    assert calculate(f, {'operation': 'outliers', 'column': 'v'})['rows'] == [{'v': 100}]
    polygon = {'type': 'Polygon', 'coordinates': [[[0, 0], [5, 0], [5, 5], [0, 5], [0, 0]], [[1, 1], [3, 1], [3, 3], [1, 3], [1, 1]]]}
    f = pd.DataFrame({'lon': [0, 2, 4, 8], 'lat': [0, 2, 4, 8], 'v': [1, 100, 3, 100]})
    r = calculate(f, {'operation': 'statistics', 'column': 'v', 'polygon': polygon, 'latitude_column': 'lat', 'longitude_column': 'lon'})
    assert r['statistics']['mean'] == 2


@pytest.mark.parametrize('name,args', [('execute_python', {'code': '1+1'}), ('query_data', {'source_id': 'x', 'operation': 'sql'}), ('query_data', {'source_id': 'x', 'operation': 'rows', 'url': 'http://127.0.0.1'})])
def test_tool_registry_rejects_code_sql_and_urls(name, args):
    with pytest.raises(Exception):
        validate_call(name, args)
