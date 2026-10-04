import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from build_nsw import build, expected_years
import pandas as pd

class NSWTests(unittest.TestCase):
    def fixture(self, root):
        entries=[]
        for sid in ('1','2','3','4'):
            entries.append({'SML_ID':sid,'HeadCampus_ID':1 if sid=='4' else int(sid),
                'SchoolName':'School '+sid,'Suburb':'Town','Postcode':'2000','State':'NSW',
                'Sector_Desc':'Government','Type_Desc':'Primary','Latitude':-33.8,'Longitude':151.1})
        (root/'directory.json').write_text(json.dumps({'schools':entries,'total':4}))
        pd.DataFrame({'ACARA SML ID':[1,2,3,4],'Location AGE ID':[101,102,103,104]}).to_excel(root/'acara-locations-2025.xlsx',sheet_name='SchoolLocations 2025',index=False)
        (root/'nsw-government.csv').write_text('AgeID,School_name,Town_suburb,Street,Date_extracted\n101,School 1,Town,1 Test Road,2026-09-04\n')
        (root/'results').mkdir()
        for sid in ('1','2','3'):
            nums=[]
            for y in (3,5):
                for d in (1,2,4,5,6):
                    if sid=='3' and y==5: continue
                    nums.append({'SML_ID':int(sid),'CAL_YR':2025,'YearLevel_ID':y,'Domain_Id':d,'SchoolAvg':400+y*10+int(sid)})
            response={'SchoolInfo':{'SML_ID':int(sid)},'Profile':{'SML_ID':int(sid),'CAL_YR':2025,'YearRange':'K-6','Percentage_LBOTE_Yes':96},'Numbers':nums}
            (root/f'results/{sid}.json').write_text(json.dumps({'data':{'response':response}}))

    def test_missing_whole_year_and_subcampus_are_not_ranked(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.fixture(root)
            data, rows=build(root,2025)
            schools={s['school_id']:s for s in data['schools']}
            self.assertTrue(data['metadata']['crawl_complete'])
            self.assertEqual(data['metadata']['responses_collected'],3)
            self.assertEqual(data['metadata']['result_school_count'],3)
            self.assertIsNotNone(schools['1']['achievement_score'])
            self.assertIsNone(schools['3']['achievement_score'])
            self.assertEqual(schools['3']['coverage'],.5)
            self.assertIsNone(schools['4']['achievement_score'])
            self.assertEqual(schools['4']['results_status'],'reported_at_head_school')
            self.assertEqual(schools['1']['address'],'1 Test Road')
            self.assertEqual(len(rows),25)

    def test_partial_collection_has_no_statewide_indices(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.fixture(root);(root/'results/3.json').unlink()
            with self.assertRaises(ValueError):build(root,2025)
            data,_=build(root,2025,True)
            self.assertFalse(data['metadata']['crawl_complete'])
            self.assertTrue(all(s['achievement_score'] is None for s in data['schools']))

    def test_downloaded_response_does_not_count_as_score_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.fixture(root)
            path=root/'results/3.json'
            payload=json.loads(path.read_text());payload['data']['response']['Numbers']=[]
            path.write_text(json.dumps(payload))
            data,_=build(root,2025)
            meta=data['metadata']
            self.assertEqual(meta['response_coverage_pct'],100)
            self.assertEqual(meta['schools_with_domain_scores'],2)
            self.assertEqual(meta['score_coverage_pct'],66.67)
            self.assertFalse(meta['score_coverage_target_met'])
            self.assertEqual(meta['score_coverage_target_pct'],80)

    def test_expected_years(self):
        self.assertEqual(expected_years('K-12'),[3,5,7,9])
        self.assertEqual(expected_years('7-12'),[7,9])
        self.assertEqual(expected_years('11-12'),[])

if __name__=='__main__': unittest.main()
