#!/usr/bin/env python3
"""Build a provenance-bearing NSW school dataset from a completed browser crawl."""
import argparse
import csv
import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from build_dataset import rank_results, WEIGHTS, YEARS

DOMAINS = {1: 'reading', 2: 'writing', 4: 'spelling', 5: 'grammar_punctuation', 6: 'numeracy'}
ROOT = Path(__file__).resolve().parents[1]
ATTRIBUTION = 'Source: Australian Curriculum, Assessment and Reporting Authority (ACARA)'


def expected_years(year_range):
    values = re.findall(r'K|P|\d+', str(year_range).upper())
    values = [0 if x in {'K', 'P'} else int(x) for x in values]
    if not values:
        return []
    return sorted(y for y in YEARS if min(values) <= y <= max(values))


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) and n >= 0 else None
    except (ValueError, TypeError):
        return None


def normal(text):
    return re.sub(r'[^a-z0-9]', '', str(text).lower())


def build(root, year, allow_partial=False):
    directory = json.loads((root/'directory.json').read_text())
    entries = directory['schools']
    if len(entries) != directory['total'] or len({str(s['SML_ID']) for s in entries}) != len(entries):
        raise ValueError('Incomplete directory')
    # The government AGE ID differs from My School's SML ID. Join explicitly
    # through ACARA's Location AGE ID, never by numerically similar identifiers.
    import pandas as pd
    locations = pd.read_excel(root/'acara-locations-2025.xlsx', sheet_name='SchoolLocations 2025')
    age_ids = {str(int(r['ACARA SML ID'])): str(int(r['Location AGE ID'])) for _, r in locations.iterrows()
               if pd.notna(r['Location AGE ID'])}
    with (root/'nsw-government.csv').open(encoding='utf-8-sig') as source:
        gov_rows = list(csv.DictReader(source))
    gov = {r['AgeID']: r for r in gov_rows}
    gov_names = {}
    for g in gov_rows:
        gov_names.setdefault((normal(g['School_name']), normal(g['Town_suburb'])), []).append(g)
    asl = {}
    if (root/'asl-addresses.json').exists():
        for a in json.loads((root/'asl-addresses.json').read_text())['schools']:
            asl[str(a['school_id'])] = a
    schools, long_rows, rank_rows, missing, seen_cells = [], [], [], [], set()
    for entry in entries:
        sid = str(entry['SML_ID'])
        head = str(entry.get('HeadCampus_ID') or sid)
        path = root/f'results/{sid}.json'
        envelope = json.loads(path.read_text()) if path.exists() else {}
        response = envelope.get('data', {}).get('response', {})
        profile = response.get('Profile') or {}
        info = response.get('SchoolInfo') or profile
        if response and str(info.get('SML_ID')) not in {sid, head}:
            raise ValueError(f'School identity mismatch: {sid}')
        if not response and sid == head:
            missing.append(sid)
        own_results = sid == head and str(info.get('SML_ID')) == sid
        current_profile = profile if profile.get('CAL_YR') == year else {}
        expected = expected_years(current_profile.get('YearRange')) if own_results else []
        g = gov.get(age_ids.get(sid, ''))
        address_match = 'acara_location_age_id' if g else None
        if not g and entry['Sector_Desc'] == 'Government':
            candidates = gov_names.get((normal(entry['SchoolName']), normal(entry['Suburb'])), [])
            if len(candidates) == 1:
                g, address_match = candidates[0], 'exact_name_and_suburb'
        a = asl.get(sid, {})
        address = (g or {}).get('Street') or a.get('address') or None
        row = {
            'school_id': sid, 'school_name': entry['SchoolName'], 'address': address,
            'suburb': entry['Suburb'], 'postcode': str(entry['Postcode']), 'state': 'NSW',
            'sector': entry['Sector_Desc'], 'school_type': entry['Type_Desc'],
            'latitude': entry.get('Latitude'), 'longitude': entry.get('Longitude'),
            'head_campus_id': head, 'campus_code': entry.get('Campus_Code'),
            'address_source': 'NSW Department of Education' if g else ('ACARA Australian Schools List' if a else None),
            'address_match': address_match if g else a.get('match'),
            'address_as_of': (g or {}).get('Date_extracted') or a.get('as_of'),
            'location_year': 2025, 'profile_year': current_profile.get('CAL_YR'),
            'year_range': current_profile.get('YearRange'), 'expected_year_levels': ','.join(map(str, expected)),
            'enrolments': number(current_profile.get('Enrollment_Total')),
            'lbote_yes_pct': number(current_profile.get('Percentage_LBOTE_Yes')),
            'lbote_no_pct': number(current_profile.get('Percentage_LBOTE_No')),
            'lbote_not_stated_pct': number(current_profile.get('Percentage_LBOTE_NotStated')),
            'naplan_participation_pct': number((response.get('NaplanParticipation') or {}).get('Participation'))
                if (response.get('NaplanParticipation') or {}).get('CAL_YR') == year and own_results else None,
            'achievement_score': None, 'rank': None, 'coverage': None, 'assessment_year': year,
            'year_levels': '', 'rank_scope': f'NSW {entry["Type_Desc"].lower()} schools with sufficient {year} results',
            'results_status': 'reported_at_head_school' if sid != head else ('not_collected' if not response else 'not_published'),
            'source_url': f'https://myschool.edu.au/school/{sid}/naplan/results',
            'retrieved_at': envelope.get('retrieved_at'),
        }
        for level in sorted(YEARS):
            for domain in WEIGHTS:
                row[f'{domain}_y{level}'] = None
        if own_results:
            cells = {}
            for r in response.get('Numbers', []):
                if r.get('CAL_YR') != year:
                    continue
                if str(r.get('SML_ID')) != sid:
                    raise ValueError(f'Result identity mismatch: {sid}')
                level, domain = r.get('YearLevel_ID'), DOMAINS.get(r.get('Domain_Id'))
                if level not in YEARS or not domain:
                    raise ValueError('Unknown result domain/year level')
                key = (sid, level, domain)
                if key in seen_cells:
                    raise ValueError(f'Duplicate result cell: {key}')
                seen_cells.add(key)
                score = number(r.get('SchoolAvg'))
                if score is not None and not 0 < score <= 1000:
                    score = None
                cells[level, domain] = score
                row[f'{domain}_y{level}'] = score
                long_rows.append({'school_id': sid, 'school_name': row['school_name'], 'assessment_year': year,
                    'year_level': level, 'domain': domain, 'mean_score': score,
                    'national_mean': number(r.get('NationalAvg')), 'similar_mean': number(r.get('SimilarAvg')),
                    'margin_lower': number(r.get('SchoolMarginLower')), 'margin_upper': number(r.get('SchoolMarginUpper')),
                    'student_count': number(r.get('Student_Count')), 'source_url': row['source_url'],
                    'retrieved_at': row['retrieved_at']})
            # Require the profile year range so an absent whole year cannot
            # silently disappear from the completeness denominator.
            if expected:
                for level in expected:
                    for domain in WEIGHTS:
                        score = cells.get((level, domain))
                        rank_rows.append({'school_id': sid, 'assessment_year': year, 'year_level': level,
                                          'domain': domain, 'mean_score': '' if score is None else str(score)})
                row['coverage'] = round(sum(WEIGHTS[d] for (y, d), v in cells.items()
                                             if v is not None and y in expected)/len(expected), 3)
            if any(v is not None for v in cells.values()):
                row['results_status'] = 'collected' if expected else 'unknown_year_range'
        schools.append(row)
    if missing and not allow_partial:
        raise ValueError(f'{len(missing)} school responses missing; finish crawl or explicitly use --allow-partial')
    scores = rank_results(rank_rows) if not missing else {}
    for row in schools:
        if row['school_id'] in scores:
            row.update(scores[row['school_id']])
            row['results_status'] = 'available'
    # Index can be read across domains/year levels; rank is restricted to
    # school type to avoid presenting primary and secondary as the same cohort.
    for school_type in {s['school_type'] for s in schools}:
        cohort = sorted((s for s in schools if s['school_type'] == school_type and s['achievement_score'] is not None),
                        key=lambda s: -s['achievement_score'])
        previous, rank = None, 0
        for i, s in enumerate(cohort, 1):
            if s['achievement_score'] != previous:
                previous, rank = s['achievement_score'], i
            s['rank'] = rank
    metadata = {
        'scope': 'NSW schools and campuses returned by My School search',
        'assessment_year': year, 'collected_at': datetime.now(timezone.utc).isoformat(),
        'directory_count': len(schools), 'crawl_complete': not missing, 'missing_school_ids': missing,
        'result_school_count': sum(s['school_id'] == s['head_campus_id'] for s in schools),
        'responses_collected': sum(s['school_id'] == s['head_campus_id'] for s in schools) - len(missing),
        'scoring_status': 'complete' if not missing else 'waiting_for_complete_crawl',
        'scored_schools': len(scores), 'result_cells': len(long_rows),
        'status_counts': dict(Counter(s['results_status'] for s in schools)),
        'address_count': sum(bool(s['address']) for s in schools),
        'location_source': 'https://myschool.edu.au/school-search?State=NSW',
        'results_source': 'https://myschool.edu.au/', 'attribution': ATTRIBUTION,
        'terms_url': 'https://myschool.edu.au/terms-of-use',
        'address_sources': ['https://data.nsw.gov.au/data/dataset/nsw-education-nsw-public-schools-master-dataset', 'https://asl.acara.edu.au/'],
        'source_sha256': hashlib.sha256((root/'directory.json').read_bytes()).hexdigest(),
        'weights': WEIGHTS,
        'method': '2025 NAPLAN school means standardised within each year level/domain across supplied NSW head schools; weighted domains (reading 30%, numeracy 30%, writing 20%, spelling 10%, grammar 10%), equal weight per expected year level, normal-CDF index 0–100. At least 80% domain weight in EVERY expected NAPLAN year level. Rank within school type; ties share rank. This is an achievement index, not a percentile or overall school-quality rating.',
        'coverage_note': 'Expected NAPLAN year levels come from the 2025 school profile year range. Suppressed and missing means stay null. Subcampuses link to their head school and are never independently ranked. Language background and participation do not contribute to the achievement score.',
    }
    heads = [s for s in schools if s['school_id'] == s['head_campus_id']]
    with_scores = sum(any(s[f'{domain}_y{level}'] is not None for domain in WEIGHTS for level in YEARS) for s in heads)
    metadata.update(schools_with_domain_scores=with_scores,
                    score_coverage_pct=round(100*with_scores/len(heads),2) if heads else 0,
                    response_coverage_pct=round(100*metadata['responses_collected']/len(heads),2) if heads else 0,
                    score_coverage_target_pct=80,
                    score_coverage_target_met=bool(heads) and with_scores/len(heads)>=.8,
                    score_coverage_denominator='NSW head schools in the source directory; campuses are not counted twice')
    status_path = root/'crawl-status.json'
    if status_path.exists():
        status = json.loads(status_path.read_text())
        metadata['crawl_status'] = 'complete' if not missing else status.get('status', 'incomplete')
        metadata['crawl_status_updated_at'] = status.get('updated_at')
        metadata['next_attempt_at'] = status.get('resume_at') if missing and status.get('status') == 'waiting_for_retry' else None
    return {'schools': schools, 'metadata': metadata}, long_rows


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False))
    temp.replace(path)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--raw', type=Path, default=ROOT/'data/raw/nsw')
    p.add_argument('--output', type=Path, default=ROOT/'public/nsw_schools.json')
    p.add_argument('--year', type=int, default=2025)
    p.add_argument('--allow-partial', action='store_true')
    args = p.parse_args()
    data, results = build(args.raw, args.year, args.allow_partial)
    write_json(args.output, data)
    write_json(args.output.with_name('nsw_naplan_results.json'), {'results': results, 'metadata': data['metadata']})
    csv_path = args.output.with_name('nsw_naplan_results.csv')
    with csv_path.open('w') as f:
        if results:
            w = csv.DictWriter(f, fieldnames=list(results[0])); w.writeheader(); w.writerows(results)
    print(json.dumps(data['metadata'], indent=2))
