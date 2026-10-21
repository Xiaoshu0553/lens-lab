from app.outcomes import compare_conditions


def rows(band,scene,group,score,n=1):
    return [dict(literacy_band=band,scenario_id=scene,group_id=group,score=score) for _ in range(n)]


def test_scenario_composition_does_not_manufacture_an_effect():
    data=rows('040-050','x','S',20,10)+rows('040-050','x','A',20,1)
    data+=rows('040-050','y','S',90,1)+rows('040-050','y','A',90,10)
    record=compare_conditions(data,['x','y'])['comparisons'][0]
    assert record['standard_mean']==record['adaptive_mean']==55
    assert record['difference_points']==0 and record['complete']


def test_missing_arm_in_a_scenario_does_not_get_replaced_by_zero_or_partial_effect():
    data=rows('040-050','x','S',20)+rows('040-050','x','A',40)+rows('040-050','y','S',50)
    record=compare_conditions(data,['x','y'])['comparisons'][0]
    assert record['difference_points'] is None and not record['complete']
    assert record['paired_scenarios']==1 and record['required_scenarios']==2


def test_effects_can_be_negative_and_literacy_gap_can_widen():
    data=[]
    for band,s,a in [('020-030',60,63),('080-090',80,95),('040-050',70,65)]:
        data+=rows(band,'one','S',s)+rows(band,'one','A',a)
    effects={r['literacy_band']:r['difference_points'] for r in compare_conditions(data,['one'])['comparisons']}
    assert effects['020-030']==3 and effects['080-090']==15
    assert effects['080-090']-effects['020-030']==12
    assert effects['040-050']==-5
