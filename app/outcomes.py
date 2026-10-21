"""Descriptive randomized-arm comparisons with fixed scenario weights.

These are between-person contrasts, not individual pre/post gains. No p-values,
validation claim, or predetermined direction is inferred from this table.
"""
from __future__ import annotations

import math
from statistics import mean


def compare_conditions(rows: list[dict], scenario_ids: list[str]) -> dict:
    scenarios = list(dict.fromkeys(scenario_ids)) or ['default']
    valid = [row for row in rows if row.get('group_id') in {'S','A'}
             and row.get('scenario_id', 'default') in scenarios
             and isinstance(row.get('score'), (int,float)) and math.isfinite(row['score'])]
    comparisons=[]
    for band in sorted({row['literacy_band'] for row in valid}):
        band_rows=[row for row in valid if row['literacy_band']==band]
        cells=[]
        for scenario in scenarios:
            cell={'scenario_id':scenario}
            for group in ['S','A']:
                scores=[row['score'] for row in band_rows if row['group_id']==group
                        and row.get('scenario_id','default')==scenario]
                cell[group]={'n':len(scores),'mean':mean(scores) if scores else None}
            cell['difference_points']=(cell['A']['mean']-cell['S']['mean']
                                       if cell['S']['n'] and cell['A']['n'] else None)
            cells.append(cell)
        complete=all(cell['difference_points'] is not None for cell in cells)
        ns={group:sum(cell[group]['n'] for cell in cells) for group in ['S','A']}
        standardized={group:mean(cell[group]['mean'] for cell in cells) if complete else None
                      for group in ['S','A']}
        comparisons.append({'literacy_band':band,'standard_n':ns['S'],'adaptive_n':ns['A'],
                            'standard_mean':round(standardized['S'],2) if complete else None,
                            'adaptive_mean':round(standardized['A'],2) if complete else None,
                            'difference_points':round(standardized['A']-standardized['S'],2) if complete else None,
                            'paired_scenarios':sum(cell['difference_points'] is not None for cell in cells),
                            'required_scenarios':len(scenarios),'complete':complete,'scenarios':cells,
                            'small_cell_warning':any(cell[g]['n']<10 for cell in cells for g in ['S','A'])})
    return {'comparisons':comparisons,'scenario_ids':scenarios,
            'weighting':'equal_scenario_weights',
            'note':'在同年龄、问卷、任务和素养区间内，先分别计算各情境的两组均分，再按预先固定的等权情境汇总。某情境缺少一组时不计算汇总差值。分差是随机平行组的描述性比较，不能称为同一人的前后提高率；正式推断须另行估计不确定性并检验连续素养与干预的交互。'}
