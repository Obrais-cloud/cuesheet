import contextlib
import io
import json
import tomllib
import unittest
from pathlib import Path

import cuesheet as cs

S = Path(__file__).parent / "samples"


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cs.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


def cues_json(*argv):
    code, out, _ = run(*argv, "--format", "json")
    assert code == 0, code
    return {(c["label"]): c for c in json.loads(out)["cues"]}


class Timecode(unittest.TestCase):
    def test_ndf_roundtrip(self):
        for tc in ("00:00:00:00", "01:00:00:00", "10:59:59:24"):
            self.assertEqual(cs.frames_to_tc(cs.tc_to_frames(tc, 25), 25), tc)

    def test_drop_frame_known_values(self):
        self.assertEqual(cs.tc_to_frames("00:01:00;02", 29.97), 1800)
        self.assertEqual(cs.tc_to_frames("00:10:00;00", 29.97), 17982)
        for f in (0, 1799, 1800, 17982, 107892):
            self.assertEqual(cs.tc_to_frames(cs.frames_to_tc(f, 29.97, True), 29.97), f)

    def test_duration(self):
        self.assertEqual(cs.frames_to_dur(25 * 83, 25), "1:23")
        self.assertEqual(cs.frames_to_dur(3, 25), "0:01")  # never report 0:00 for a real use


class Edl(unittest.TestCase):
    def setUp(self):
        self.c = cues_json(S / "fdv_video.edl", S / "fdv_audio.edl",
                           "-r", S / "fdv_rights.toml")

    def test_stereo_pair_and_contiguous_edit_are_one_cue(self):
        m1 = self.c["M01"]
        self.assertEqual((m1["tc_in"], m1["tc_out"]), ("01:00:00:00", "01:00:45:00"))
        self.assertEqual(m1["tracks"], ["A1", "A2"])

    def test_gap_splits_cues_unless_merge_gap(self):
        self.assertIn("M03", self.c)
        merged = cues_json(S / "fdv_audio.edl", "-r", S / "fdv_rights.toml",
                           "--merge-gap", "3")
        self.assertNotIn("M03", merged)
        self.assertEqual(merged["M02"]["seconds"], 40.0)

    def test_sync_sound_is_not_music(self):
        self.assertFalse(any("interview" in c["source"] for c in self.c.values()))

    def test_dissolve_uses_incoming_clip_and_black_is_ignored(self):
        self.assertEqual(self.c["AR03"]["source"], "ARCH_NODO_curros_1962.mov")
        self.assertFalse(any(c["source"] in ("BL", "BLK") for c in self.c.values()))

    def test_rights_metadata_and_missing(self):
        self.assertEqual(self.c["M01"]["meta"]["pro"], "SGAE")
        self.assertEqual(self.c["M02"]["missing"], ["publisher"])
        self.assertEqual(self.c["ST01"]["missing"], ["owner", "license"])


class Fcpxml(unittest.TestCase):
    def setUp(self):
        self.c = cues_json(S / "fdv_fcp.fcpxml")

    def test_connected_music_may_outlast_parent(self):
        m = self.c["M01"]
        self.assertEqual((m["tc_in"], m["tc_out"]), ("01:00:02:00", "01:00:32:00"))

    def test_compound_is_clipped_and_merged_with_adjacent_use(self):
        ar = self.c["AR01"]
        self.assertEqual((ar["tc_in"], ar["tc_out"]), ("01:00:10:00", "01:00:19:00"))
        self.assertNotIn("AR02", self.c)

    def test_own_footage_and_disabled_clips_ignored(self):
        self.assertEqual(set(self.c), {"M01", "AR01"})


class Cli(unittest.TestCase):
    def test_strict_fails_on_missing_rights(self):
        code, _, err = run(S / "fdv_video.edl", S / "fdv_audio.edl",
                           "-r", S / "fdv_rights.toml", "--strict")
        self.assertEqual(code, 2)
        self.assertIn("STOCK_pond5_horses_fog.mov", err)

    def test_init_is_valid_toml_and_feeds_back(self):
        code, out, _ = run(S / "fdv_video.edl", S / "fdv_audio.edl",
                           "-r", S / "fdv_rights.toml", "--init")
        self.assertEqual(code, 0)
        data = tomllib.loads(out)
        self.assertEqual(data["sources"]["MUS_Cantiga_do_vento.wav"]["composer"],
                         "Compositora Ejemplo")  # keeps what you already filled in
        self.assertIn("ARCH_NODO_curros_1962.mov", data["sources"])
        self.assertIn('# [sources."A001_C003_FDV.mov"]', out)

    def test_csv_and_only(self):
        code, out, _ = run(S / "fdv_video.edl", "--format", "csv", "--only", "archive")
        self.assertEqual(code, 0)
        rows = out.strip().splitlines()
        self.assertTrue(rows[0].startswith("cue,category,source"))
        self.assertTrue(all(",archive," in r for r in rows[1:]))
        self.assertEqual(len(rows), 4)


if __name__ == "__main__":
    unittest.main()
