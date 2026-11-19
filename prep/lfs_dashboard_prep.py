# LFS 2020-21 -> Power BI tables for the "District Fingerprint" dashboard
# Usage: python lfs_dashboard_prep.py <LFS2020-21.dta or lfs2021_slim.csv.gz> <output_dir>
#
# Variable codes (PBS LFS 2020-21).
#   S4C5  sex (1 male, 2 female)          S4C6  age            Region 1 rural, 2 urban
#   S4C10 level currently enrolled; 1 = not enrolled (confirmed)
#   S5C1/S5C2/S5C3 == 1 -> employed (asked age 10+), same rule as lfs2021_disability_lfp.py
#   S9C1/S9C5/S9C6 -> ILO unemployment (no work, available, seeking / future starter)
#   S5C161 + S5C251 -> weekly hours, main + secondary job
#   Weights = expansion weight; EBCode = enumeration block (PSU)
import sys, os
import numpy as np, pandas as pd

SRC, OUT = sys.argv[1], sys.argv[2]
os.makedirs(OUT, exist_ok=True)
COLS = ['EBCode', 'Province', 'District', 'Region', 'S4C5', 'S4C6', 'S4C10', 'S5C1', 'S5C2', 'S5C3',
        'S5C161', 'S5C251', 'S9C1', 'S9C5', 'S9C6', 'Weights']
if SRC.endswith('.dta'):
    import pyreadstat
    df, _ = pyreadstat.read_dta(SRC, usecols=COLS, encoding='latin1', apply_value_formats=False)
else:
    df = pd.read_csv(SRC, usecols=COLS, low_memory=False)

PROV = {1: 'Khyber Pakhtunkhwa', 2: 'Punjab', 3: 'Sindh', 4: 'Balochistan'}
d = df.copy()
for c in COLS:
    if c not in ('District', 'EBCode'):
        d[c] = pd.to_numeric(d[c], errors='coerce')
d = d[d.Weights > 0].copy()
d['district'] = d.District.str.strip().str.upper()        # source mixes cases (e.g. 'Mardan' / 'MARDAN')
# 'EAST' (n=33) and 'WEST' (n=44) are stray Karachi labels; fold into the Karachi districts
d['district'] = d.district.replace({'EAST': 'KARACHI EAST', 'WEST': 'KARACHI WEST'})
d['province'] = d.Province.map(PROV)
d['sex'] = np.where(d.S4C5 == 2, 'Female', 'Male')
d['age'] = d.S4C6
d['w'] = d.Weights
d['psu'] = d.EBCode.astype(str)
d['attend'] = (d.S4C10 >= 2).astype(float)
d.loc[d.S4C10.isna(), 'attend'] = np.nan
emp = (d.S5C1 == 1) | (d.S5C2 == 1) | (d.S5C3 == 1)
unemp = (~emp) & d.S9C6.isin([1, 2]) & ((d.S9C1 == 1) | (d.S9C5 == 1))
d['emp'] = emp.astype(float)
d['unemp'] = unemp.astype(float)
d['lf'] = (emp | unemp).astype(float)
d.loc[d.age < 10, ['emp', 'unemp', 'lf']] = np.nan
d['hours'] = d.S5C161.fillna(0) + d.S5C251.fillna(0)

# ---------------------------------------------------------------- indicator definitions
# (id, short name, long name, theme, universe mask, outcome column, need direction, SDG ref)
def a(lo, hi):
    return (d.age >= lo) & (d.age <= hi)

d['oos'] = 1 - d.attend
d['work_oos'] = d.emp * d.oos
d['neet'] = ((d.emp == 0) & (d.attend == 0)).astype(float)
d.loc[d.emp.isna() | d.attend.isna(), 'neet'] = np.nan

IND = [
    ('C1', 'Out of school (10-14)', 'Children aged 10-14 not currently attending school', 'Education', a(10, 14), 'oos', 1, 'SDG 4.1'),
    ('C2', 'Out of school (15-17)', 'Adolescents aged 15-17 not currently attending school', 'Education', a(15, 17), 'oos', 1, 'SDG 4.1'),
    ('L1', 'Working children (10-14)', 'Children aged 10-14 in employment (any work in reference week)', 'Child work', a(10, 14), 'emp', 1, 'SDG 8.7'),
    ('L2', 'Working adolescents (15-17)', 'Adolescents aged 15-17 in employment', 'Child work', a(15, 17), 'emp', 1, 'SDG 8.7'),
    ('L3', 'Working and out of school (10-17)', 'Children aged 10-17 working and not attending school', 'Child work', a(10, 17), 'work_oos', 1, 'SDG 8.7'),
    ('Y1', 'Youth NEET (15-24)', 'Youth aged 15-24 not in employment or education', 'Youth', a(15, 24), 'neet', 1, 'SDG 8.6.1'),
    ('Y2', 'Youth unemployment (15-24)', 'Unemployed as share of youth labour force, 15-24 (ILO definition)', 'Youth', a(15, 24) & (d.lf == 1), 'unemp', 1, 'SDG 8.5.2'),
    ('W1', 'Female labour force participation (15-64)', 'Women aged 15-64 in the labour force (employed or unemployed)', "Women's work", a(15, 64) & (d.sex == 'Female'), 'lf', -1, 'SDG 8.5'),
]
GAPS = [('G1', 'Gender gap in youth NEET', 'Female minus male NEET rate, 15-24 (percentage points)', 'Gender gaps', 'Y1', 1, 'SDG 5'),
        ('G2', 'Gender gap in out-of-school (10-14)', 'Female minus male out-of-school rate, 10-14 (percentage points)', 'Gender gaps', 'C1', 1, 'SDG 4.5')]

# ---------------------------------------------------------------- design-based estimation
def est(sub, y):
    """Weighted proportion with linearised SE: PSUs = EBCode, strata = district."""
    s = sub[sub[y].notna()]
    n = len(s)
    if n == 0 or s.w.sum() == 0:
        return dict(value=np.nan, se=np.nan, n=0, pop=0.0)
    W = s.w.sum()
    p = (s.w * s[y]).sum() / W
    z = s.w * (s[y] - p) / W
    zc = z.groupby([s.district, s.psu]).sum()
    var = 0.0
    for _, g in zc.groupby(level=0):
        k = len(g)
        if k > 1:
            var += k / (k - 1) * ((g - g.mean()) ** 2).sum()
    return dict(value=p, se=np.sqrt(var), n=n, pop=W)

def reliability(n, se):
    if n < 50:
        return 'Suppressed'
    if n < 100 or 1.96 * se > 0.10:
        return 'Use with caution'
    return 'Reliable'

geos = [('National', 'Pakistan', 'Pakistan', d)]
geos += [('Province', p, p, g) for p, g in d.groupby('province')]
geos += [('District', k, g.province.iloc[0], g) for k, g in d.groupby('district')]

rows = []
for lvl, key, prov, g in geos:
    for iid, sname, lname, theme, mask, y, direc, sdg in IND:
        u = g[mask.loc[g.index]]
        for sx in ['All', 'Female', 'Male']:
            if iid == 'W1' and sx == 'Male':
                continue
            s = u if sx == 'All' else u[u.sex == sx]
            r = est(s, y)
            rows.append(dict(geo_level=lvl, geo_key=key, province=prov, indicator_id=iid, sex=sx, **r))
F = pd.DataFrame(rows)

# gender gaps (F - M), SE assumes independence between sexes
gap_rows = []
for gid, _, _, _, base, _, _ in GAPS:
    b = F[F.indicator_id == base].pivot_table(index=['geo_level', 'geo_key', 'province'], columns='sex',
                                              values=['value', 'se', 'n'], aggfunc='first')
    for idx, r in b.iterrows():
        gap_rows.append(dict(geo_level=idx[0], geo_key=idx[1], province=idx[2], indicator_id=gid, sex='All',
                             value=r[('value', 'Female')] - r[('value', 'Male')],
                             se=np.sqrt(r[('se', 'Female')] ** 2 + r[('se', 'Male')] ** 2),
                             n=min(r[('n', 'Female')], r[('n', 'Male')]), pop=np.nan))
F = pd.concat([F, pd.DataFrame(gap_rows)], ignore_index=True)

F['reliability'] = [reliability(n, se) for n, se in zip(F.n, F.se)]
F['ci_lo'] = F.value - 1.96 * F.se
F['ci_hi'] = F.value + 1.96 * F.se
isgap = F.indicator_id.str.startswith('G')
F.loc[~isgap, 'ci_lo'] = F.loc[~isgap, 'ci_lo'].clip(lower=0)
F.loc[~isgap, 'ci_hi'] = F.loc[~isgap, 'ci_hi'].clip(upper=1)
for c in ['value', 'se', 'ci_lo', 'ci_hi']:
    F[c] = (F[c] * 100).round(2)          # percent / percentage points
F['est_count'] = np.where(isgap, np.nan, (F.value / 100 * F['pop']).round(0))
F['value_shown'] = np.where(F.reliability == 'Suppressed', np.nan, F.value)
F = F.drop(columns='pop')
F.to_csv(f'{OUT}/fact_indicators.csv', index=False)

# ---------------------------------------------------------------- indicator dimension
DI = pd.DataFrame([dict(indicator_id=i, short_name=s, long_name=l, theme=t, need_direction=dr, sdg=sd,
                        unit='%', sort=k) for k, (i, s, l, t, _, _, dr, sd) in enumerate(IND)] +
                  [dict(indicator_id=i, short_name=s, long_name=l, theme=t, need_direction=dr, sdg=sd,
                        unit='pp', sort=len(IND) + k) for k, (i, s, l, t, _, dr, sd) in enumerate(GAPS)])
DI.to_csv(f'{OUT}/dim_indicator.csv', index=False)

# ---------------------------------------------------------------- district scores (fingerprint)
dirn = DI.set_index('indicator_id').need_direction
D = F[(F.geo_level == 'District') & (F.sex == 'All')].copy()
D = D.rename(columns={'geo_key': 'district'})
D['need_value'] = D.value_shown * D.indicator_id.map(dirn)
D['need_pctile_national'] = D.groupby('indicator_id').need_value.rank(pct=True) * 100
D['need_pctile_province'] = D.groupby(['indicator_id', 'province']).need_value.rank(pct=True) * 100
prov = F[(F.geo_level == 'Province') & (F.sex == 'All')].set_index(['geo_key', 'indicator_id']).value
nat = F[(F.geo_level == 'National') & (F.sex == 'All')].set_index('indicator_id').value
D['province_value'] = [prov.get((p, i)) for p, i in zip(D.province, D.indicator_id)]
D['national_value'] = D.indicator_id.map(nat)
D['diff_vs_province'] = (D.value - D.province_value).round(2)
D['priority_flag'] = ((D.need_pctile_national >= 75) & D.value_shown.notna()).astype(int)
D = D[['district', 'province', 'indicator_id', 'value', 'value_shown', 'ci_lo', 'ci_hi', 'n', 'reliability',
       'need_pctile_national', 'need_pctile_province', 'province_value', 'national_value', 'diff_vs_province',
       'priority_flag']].round(2)
D.to_csv(f'{OUT}/district_scores.csv', index=False)

S = D.groupby(['district', 'province']).agg(priority_count=('priority_flag', 'sum'),
                                             indicators_reported=('value_shown', 'count'),
                                             mean_need_pctile=('need_pctile_national', 'mean')).reset_index()
S['priority_rank_national'] = S.mean_need_pctile.rank(ascending=False, method='min').astype('Int64')
S['priority_rank_province'] = S.groupby('province').mean_need_pctile.rank(ascending=False, method='min').astype('Int64')
S['n_districts_province'] = S.groupby('province').district.transform('count')

# headline sentence: the district's highest-need indicator vs its province
lab = DI.set_index('indicator_id').short_name
def headline(g):
    name = g.name.title()
    g = g[g.value_shown.notna() & ~g.indicator_id.str.startswith('G')]
    if g.empty:
        return ''
    r = g.sort_values('need_pctile_national', ascending=False).iloc[0]
    rel = 'above' if r.value > r.province_value else 'below'
    return (f"{name}'s highest-need indicator is {lab[r.indicator_id]}: {r.value:.1f}%, "
            f"{rel} the {r.province} average of {r.province_value:.1f}%.")
S = S.merge(D.groupby('district').apply(headline, include_groups=False).rename('headline').reset_index(), on='district')
S['mean_need_pctile'] = S.mean_need_pctile.round(1)
S.to_csv(f'{OUT}/district_summary.csv', index=False)

# ---------------------------------------------------------------- school-to-work transition (ages 10-24)
# status shares by single year of age, 3-year moving window (age-1..age+1) to stabilise district cells
t = d[a(9, 25) & d.attend.notna() & d.emp.notna()].copy()
t['status'] = np.select([(t.attend == 1) & (t.emp == 0), (t.attend == 1) & (t.emp == 1), (t.attend == 0) & (t.emp == 1)],
                        ['In school only', 'In school and working', 'Working only'], 'Neither')
ST = ['In school only', 'In school and working', 'Working only', 'Neither']
trows = []
units = [('National', 'Pakistan', t)] + [('Province', p, g) for p, g in t.groupby('province')] + \
        [('District', k, g) for k, g in t.groupby('district')]
for lvl, key, g in units:
    for sx in ['All', 'Female', 'Male']:
        s0 = g if sx == 'All' else g[g.sex == sx]
        for age in range(10, 25):
            s = s0[(s0.age >= age - 1) & (s0.age <= age + 1)]
            W = s.w.sum()
            for st in ST:
                trows.append(dict(geo_level=lvl, geo_key=key, sex=sx, age=age, status=st,
                                  share=round((s.w * (s.status == st)).sum() / W * 100, 2) if W > 0 else np.nan,
                                  n_window=len(s)))
pd.DataFrame(trows).to_csv(f'{OUT}/transition.csv', index=False)

# ---------------------------------------------------------------- district dimension
dim = d.groupby('district').agg(province=('province', 'first'), n_persons=('w', 'size'),
                                population_est=('w', 'sum')).reset_index()
dim['district_name'] = dim.district.str.title().str.replace(' Of ', ' of ')
dim['population_est'] = dim.population_est.round(0)
dim.to_csv(f'{OUT}/dim_district.csv', index=False)

print('districts:', len(dim), '| indicator rows:', len(F), '| transition rows:', len(trows))
print(F[F.geo_level != 'District'].query("sex=='All'").pivot(index='indicator_id', columns='geo_key', values='value').round(1).to_string())
print(F[F.geo_level == 'District'].reliability.value_counts().to_string())
