import unittest

import helpers  # noqa: F401
from helpers import real
from mcdevlab_viewer import logs as L


class Normalise(unittest.TestCase):
    def test_variable_parts_vanish(self):
        a = "Chunk 12 at 0x7fa3b2c1d4e8 took 51 ms in 2026-10-05T07:01:01.727Z uuid 123e4567-e89b-12d3-a456-426614174000 id deadbeef12"
        b = "Chunk 977 at 0x1111111111 took 7 ms in 2026-10-06T01:02:03.000Z uuid 00000000-0000-0000-0000-000000000001 id cafebabe99"
        self.assertEqual(L.normalize(a), L.normalize(b))

    def test_run_dir_and_mac_and_versions(self):
        a = "bad pack /root/cubeworld-runtime/feature-plan/feature-plan-mcdevlab-20261005T065523Z/game/resourcepacks/x v1.2.3.zip"
        b = "bad pack /root/cubeworld-runtime/feature-plan/feature-plan-on-20261005-r9/game/resourcepacks/x v4.5.6.zip"
        self.assertEqual(L.normalize(a), L.normalize(b))
        self.assertEqual(L.normalize("random bytes: 3e:3f:7b:1:4c:f2:b8:aa"), L.normalize("random bytes: 00:11:22:33:44:55:66:77"))

    def test_different_words_stay_different(self):
        self.assertNotEqual(L.normalize("Failed to load recipe alpha"), L.normalize("Failed to load recipe beta"))


class Reading(unittest.TestCase):
    def test_xml_then_plain(self):
        text = real("client-log-head.log")
        lines = L.to_lines(text)
        self.assertTrue(lines[0].startswith("[06:55:24.490] [main/INFO] [cpw.mods.modlauncher.Launcher]"))
        self.assertGreater(len(lines), 1000)
        self.assertTrue(any("/ERROR]" in ln for ln in lines))
        self.assertFalse(any("<log4j:Event" in ln for ln in lines[:200]))

    def test_xml_message_with_multiline_and_throwable(self):
        xml = ('  <log4j:Event logger="a.B" timestamp="1791183324490" level="ERROR" thread="main">\n'
               '    <log4j:Message><![CDATA[boom\nsecond line]]></log4j:Message>\n'
               '    <log4j:Throwable><![CDATA[java.lang.IllegalStateException: x\n\tat a.B.c(B.java:1)]]></log4j:Throwable>\n'
               '  </log4j:Event>\n[01:02:03] [Render thread/INFO] [m/]: after\n')
        lines = L.to_lines(xml)
        self.assertEqual(lines[0], "[06:55:24.490] [main/ERROR] [a.B]: boom")
        self.assertEqual(lines[1], "second line")
        self.assertEqual(lines[2], "java.lang.IllegalStateException: x")
        self.assertEqual(lines[-1], "[01:02:03] [Render thread/INFO] [m/]: after")

    def test_header_variants(self):
        for ln in ("[05Oct2026 00:01:06.533] [Render thread/INFO] [io.x.Y/]: msg", "[00:09:04] [Render thread/ERROR] [fzzy_config/]: Mod x", "[12:00:00] [main/INFO]: vanilla"):
            self.assertIsNotNone(L.parse_header(ln), ln)
        self.assertIsNone(L.parse_header("\tat java.base/java.lang.Thread.run(Thread.java:1)"))


class Diff(unittest.TestCase):
    BASE = ["[00:00:01] [main/INFO] [x/]: fine",
            "[00:00:02] [Render thread/WARN] [examplemod/]: Chunk 12 took 51 ms at 0xAAAA1111FFFF",
            "[00:00:03] [Render thread/ERROR] [minecraft/RecipeManager]: Parsing error loading recipe old",
            "[00:00:04] [Render thread/WARN] [gone/]: only in baseline"]
    CAND = ["[00:00:01] [main/INFO] [x/]: fine",
            "[00:09:02] [Render thread/WARN] [examplemod/]: Chunk 99 took 77 ms at 0xBBBB2222EEEE",
            "[00:09:03] [Render thread/ERROR] [minecraft/RecipeManager]: Parsing error loading recipe old",
            "[00:09:04] [Render thread/ERROR] [examplemod/]: Mesh upload failed for chunk 7",
            "java.lang.IllegalStateException: no context 55",
            "\tat com.example.Mesher.run(Mesher.java:3)",
            "[00:09:05] [Render thread/INFO] [x/]: info is never diffed"]

    def test_only_new_lines_after_normalising(self):
        d = L.diff_logs(self.BASE, self.CAND)
        sigs = [g["sig"] for g in d["new"]]
        self.assertEqual(len(sigs), 2)
        self.assertTrue(any(s.startswith("ERROR [examplemod/] Mesh upload failed") for s in sigs))
        self.assertTrue(any(s.startswith("EXC java.lang.IllegalStateException: no context") for s in sigs))
        self.assertEqual(d["new_total"], 2)
        self.assertEqual([g["sig"].split("]")[0] for g in d["resolved"]], ["WARN [gone/"])

    def test_counts_are_grouped(self):
        cand = self.CAND + ["[00:09:06] [Render thread/ERROR] [examplemod/]: Mesh upload failed for chunk 8",
                            "[00:09:07] [Render thread/ERROR] [examplemod/]: Mesh upload failed for chunk 9"]
        d = L.diff_logs(self.BASE, cand)
        g = next(x for x in d["new"] if x["sig"].startswith("ERROR [examplemod/] Mesh"))
        self.assertEqual(g["count"], 3)
        self.assertEqual(d["new"][0]["level"], "ERROR")   # errors sort first

    def test_increase_reported_separately(self):
        base = ["[00:00:01] [r/WARN] [m/]: noisy 1"]
        cand = ["[00:00:01] [r/WARN] [m/]: noisy %d" % i for i in range(12)]
        d = L.diff_logs(base, cand)
        self.assertEqual(d["new_total"], 0)
        self.assertEqual(d["increased"][0]["base_count"], 1)
        self.assertEqual(d["increased"][0]["count"], 12)

    def test_real_logs_identical_have_no_diff(self):
        lines = L.to_lines(real("client-log-head.log"))
        d = L.diff_logs(lines, lines)
        self.assertEqual((d["new_total"], d["resolved_total"], d["increased_total"]), (0, 0, 0))

    def test_levels(self):
        c = L.count_levels(self.CAND)
        self.assertEqual((c["warn"], c["error"], c["exceptions"]), (1, 2, 1))


class Crash(unittest.TestCase):
    def test_mod_frame_culprit(self):
        lines = ["[00:01:20] [Render thread/ERROR] [minecraft/Minecraft]: Unhandled game exception",
                 "java.lang.NullPointerException: level is null",
                 "\tat net.minecraft.client.renderer.LevelRenderer.renderLevel(LevelRenderer.java:1200) ~[client.jar:?] {re:mixin}",
                 "\tat com.example.examplemod.render.ChunkMesher.mesh(ChunkMesher.java:88) ~[examplemod-1.0.0.jar%23101!/:?] {re:classloading}",
                 "[00:01:21] [main/INFO] [x/]: bye"]
        c = L.crash_signature(lines)
        self.assertEqual(c["exception"], "java.lang.NullPointerException")
        self.assertEqual(c["culprit"]["kind"], "mod-frame")
        self.assertEqual(c["culprit"]["mod"], "examplemod")
        self.assertFalse(c["fatal"])

    def test_mixin_error_names_the_mod(self):
        lines = ["[00:00:05] [main/ERROR] [mixin/]: Mixin apply failed",
                 "org.spongepowered.asm.mixin.transformer.throwables.MixinTransformerError: Mixin [foo.mixins.json:BarMixin from mod foomod] failed",
                 "Caused by: org.spongepowered.asm.mixin.injection.throwables.InvalidInjectionException: Critical injection failure",
                 "\tat org.spongepowered.asm.mixin.transformer.MixinProcessor.applyMixins(MixinProcessor.java:1) ~[mixin.jar:?]"]
        c = L.crash_signature(lines)
        self.assertEqual(c["culprit"]["kind"], "mixin")
        self.assertEqual(c["culprit"]["mod"], "foomod")
        self.assertIn("InvalidInjectionException", c["root_cause"])

    def test_frame_suffix_listing_every_mixin_is_not_a_culprit(self):
        # the frame tail lists every mixin that touched the class; that must not be read as the cause
        lines = ["java.net.UnknownHostException: sessionserver",
                 "\tat net.minecraft.client.Minecraft.lambda$new$1(Minecraft.java:426) ~[client.jar:?] {pl:mixin:APP:x.mixins.json:MinecraftMixin from mod other_mod}"]
        c = L.crash_signature(lines)
        self.assertIsNone(c["culprit"]["mod"])

    def test_fatal_marker_and_none(self):
        lines = ["[00:00:01] [main/INFO] [x/]: ---- Minecraft Crash Report ----", "[00:00:02] [main/INFO] [x/]: fine"]
        c = L.crash_signature(lines)
        self.assertTrue(c["fatal"])
        self.assertIsNone(c["exception"])
        self.assertIsNone(L.crash_signature(["[00:00:01] [main/INFO] [x/]: clean"]))

    def test_real_log_first_exception_and_mods(self):
        lines = L.to_lines(real("client-log-head.log"))
        c = L.crash_signature(lines)
        self.assertIsNotNone(c["exception"])
        mods = L.parse_mod_list(lines)
        self.assertGreater(len(mods), 100)
        self.assertEqual(L.log_environment(lines)["window"], [1280, 800])


if __name__ == "__main__":
    unittest.main()
