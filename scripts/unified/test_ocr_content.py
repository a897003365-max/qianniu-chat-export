import copy
import unittest

from ocr_content import group_ocr_versions, ocr_content_fingerprint


class Tests(unittest.TestCase):
    def test_only_frame_timing_is_ignored_and_all_occurrences_remain(self):
        first = {"image_sha256":"image-a","engine":"fixture","version":"1",
                 "frames":[{"text":"same","entries":[{"box":[[1,2]],"confidence":0.8}],"elapsed_seconds":[1,2]}]}
        second = copy.deepcopy(first)
        second['frames'][0]['elapsed_seconds'] = [9,10]
        before = copy.deepcopy(first)
        items = [{'value':first,'source':'a'},{'value':second,'source':'b'},{'value':first,'source':'copied-a'}]
        groups = group_ocr_versions(items)
        self.assertEqual(len(groups),1)
        self.assertEqual(next(iter(groups.values())),items)
        self.assertEqual(first,before)
        for field in ('image_sha256','engine','version','unknown_field','elapsed_seconds'):
            changed = copy.deepcopy(first)
            changed[field] = 'different'
            self.assertNotEqual(ocr_content_fingerprint(first),ocr_content_fingerprint(changed),field)
        for mutation in ('text','box','confidence','frame_order'):
            changed = copy.deepcopy(first)
            if mutation=='text':changed['frames'][0]['text']='different'
            if mutation=='box':changed['frames'][0]['entries'][0]['box']=[[3,4]]
            if mutation=='confidence':changed['frames'][0]['entries'][0]['confidence']=0.9
            if mutation=='frame_order':changed['frames'].insert(0,{'text':'other'})
            self.assertNotEqual(ocr_content_fingerprint(first),ocr_content_fingerprint(changed),mutation)


if __name__ == '__main__':
    unittest.main()
