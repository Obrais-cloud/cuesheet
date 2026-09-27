import contextlib
import io
import json
import tempfile
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


class Fcp7Xml(unittest.TestCase):
    """Structure mirrors real Resolve 'FCP 7 XML' exports (xmeml v5)."""

    def setUp(self):
        self.c = cues_json(S / "fdv_fcp7.xml")

    def test_rate_and_start_tc_from_sequence(self):
        code, out, _ = run(S / "fdv_fcp7.xml", "--format", "json")
        tl = json.loads(out)["timeline"]
        self.assertEqual((tl["fps"], tl["start_tc"]), (23.976, "01:00:00:00"))

    def test_transition_edges_and_file_refs_by_id(self):
        ar1, ar2 = self.c["AR01"], self.c["AR02"]
        # centred dissolve 240-264 -> edit point 252 (what Resolve reports)
        self.assertEqual((ar1["tc_in"], ar1["tc_out"]), ("01:00:10:12", "01:00:20:00"))
        self.assertEqual((ar2["tc_in"], ar2["tc_out"]), ("01:00:25:00", "01:00:30:00"))
        self.assertEqual(ar2["path"], "/Volumes/FDV/Archivo TVG/ARCH_curros_1962.mov")

    def test_stereo_music_one_cue_and_disabled_or_generators_skipped(self):
        self.assertEqual(self.c["M01"]["tracks"], ["A1", "A2"])
        self.assertEqual(self.c["M01"]["seconds"], 30.03)
        self.assertEqual(set(self.c), {"M01", "AR01", "AR02"})


class TransitionEditPoint(unittest.TestCase):
    """Alignments seen in a real Resolve FCP 7 XML export (center/start/end-black)."""

    def cut(self, align):
        import xml.etree.ElementTree as ET
        return cs._xm_cut(ET.fromstring(
            f"<transitionitem><start>100</start><end>150</end>"
            f"<alignment>{align}</alignment></transitionitem>"))

    def test_alignments(self):
        self.assertEqual(self.cut("center"), 125)
        self.assertEqual(self.cut("start"), 100)
        self.assertEqual(self.cut("start-black"), 100)
        self.assertEqual(self.cut("end-black"), 150)


class DefaultRules(unittest.TestCase):
    """Names and folders as they appear in a real feature timeline (Resolve 21)."""

    def cat(self, name, path="", kind="audio", sources=None):
        rules = cs.load_rules(None)
        if sources:
            rules["sources"] = sources
        return cs.classify(cs.Event(name, path, kind, "A1", 0, 10), rules)

    def test_music_needs_evidence(self):
        self.assertEqual(self.cat("Alex Aller - Baixada.mp3",
                                  "/V/A RAPA/04_Music/A Rapa (BSO - Demos)/Alex Aller - Baixada.mp3"), "music")
        self.assertEqual(self.cat("O Monte - riser.wav",
                                  "/V/Alex Aller Manipulated Score/O Monte - riser.wav"), "music")
        self.assertEqual(self.cat("FDV_SOUND_OST_18min.wav", "/V/x/FDV_SOUND_OST_18min.wav"), "music")
        self.assertNotEqual(self.cat("Postproduction_host.wav", "/V/x/Postproduction_host.wav"), "music")

    def test_sfx_libraries(self):
        self.assertEqual(self.cat("Bass Boom Rumbling.wav",
                                  "/V/02_SFX/Foley SFX/Bass Boom Rumbling.wav"), "sfx")
        self.assertEqual(self.cat("Lluvia.mp3", "/V/USB/EFECTOS DE SONIDO/Lluvia.mp3"), "sfx")

    def test_production_sound_is_never_a_cue(self):
        for name, path in [
            ("ZOOM0040_Tr1.WAV", "/V/Audio/ZOOM0040/ZOOM0040_Tr1.WAV"),
            ("200310_001.WAV", "/V/Audio/Zoom F2/200310_001.WAV"),
            ("070318 DIA1 SABUCEDO-T017.WAV", "/V/Audio/070318 DIA1 SABUCEDO-T017.WAV"),
            ("A005_C009_0706BB_A01_001.wav", "/V/RED/A005_C009_0706BB.RDC/A005_C009_0706BB_A01_001.wav"),
        ]:
            self.assertIsNone(self.cat(name, path), name)

    def test_unknown_audio_is_flagged_for_review_and_can_be_ignored(self):
        path = "/V/MEZCLA/C028_PCM_ORIGINAL_4CH_v001.wav"
        self.assertEqual(self.cat("C028_PCM_ORIGINAL_4CH_v001.wav", path), "audio-review")
        self.assertIsNone(self.cat("C028_PCM_ORIGINAL_4CH_v001.wav", path,
                                   sources={"C028_PCM_ORIGINAL_4CH_v001.wav": {"category": ""}}))

    def test_init_for_review_sources_is_valid_toml(self):
        tl = cs.Timeline("t", 24, False, 0, [cs.Event("X_PCM.wav", "/V/X_PCM.wav", "audio", "A1", 0, 48)])
        data = tomllib.loads(cs.render_init(tl, cs.load_rules(None)))
        self.assertEqual(data["sources"]["X_PCM.wav"]["category"], "audio-review")
        # feeding that file back must still flag the source as undecided
        rules = cs.load_rules(None)
        rules["sources"] = data["sources"]
        self.assertEqual(cs.build_cues(tl, rules)[0].missing, ["category"])


class BadInput(unittest.TestCase):
    def test_unknown_xml_is_an_error_not_an_empty_report(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.xml"
            p.write_text("<?xml version='1.0'?><project><thing/></project>")
            code, out, err = run(p)
        self.assertEqual(code, 1)
        self.assertIn("unsupported XML", err)
        self.assertEqual(out, "")

    def test_timeline_with_no_events_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.edl"
            p.write_text("TITLE: nothing here\nFCM: NON-DROP FRAME\n")
            code, _, err = run(p)
        self.assertEqual(code, 1)
        self.assertIn("no clip events", err)


if __name__ == "__main__":
    unittest.main()
