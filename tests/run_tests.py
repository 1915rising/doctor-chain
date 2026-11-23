"""Driver tests for doctor_chain.py -- no Blender required.

The five doctors are replaced by fakes in a temp tools dir, and
Blender by a fake executable that mimics clean_ai_mesh.py and
chain_blender_stage.py contracts. What is under test is the
orchestrator itself: stage order, skipping, verdict mapping,
stop-on-failure with evidence preserved, and the combined report.

    python3 tests/run_tests.py
"""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import doctor_chain as dc  # noqa: E402

FAKE_RIPDOCTOR = '''\
import json, os, sys
argv = sys.argv[1:]
out = argv[argv.index("--json") + 1]
md = argv[argv.index("--md") + 1]
Path = __import__("pathlib").Path
Path(out).write_text(json.dumps({"target": argv[0], "kind": "fbx",
    "counts": {"meshes": 1}, "checks": []}))
Path(md).write_text("# fake ripdoctor\\n")
sys.exit(int(os.environ.get("FAKE_RIP_EXIT", "0")))
'''

FAKE_STL = '''\
import json, os, sys
argv = sys.argv[1:]
out = argv[argv.index("-o") + 1]
code = int(os.environ.get("FAKE_STL_EXIT", "0"))
__import__("pathlib").Path(out).write_text(json.dumps([{
    "file": argv[-1], "triangles": 12, "components": 1,
    "watertight": code == 0, "boundary_edges": 0,
    "nonmanifold_edges": 0, "degenerate_faces": 0,
    "healthy": code == 0, "problems": []}]))
sys.exit(code)
'''

FAKE_BLENDER = '''\
#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
argv = sys.argv[1:]
script = argv[argv.index("--python") + 1]
rest = argv[argv.index("--") + 1:]
if script.endswith("clean_ai_mesh.py"):
    args = dict(zip(rest[::2], rest[1::2]))
    if os.environ.get("FAKE_CLEANUP_MODE") == "fail":
        print("fake cleanup: dying quietly, like real Blender")
        sys.exit(0)
    Path(args["--output"]).write_bytes(b"glTF-fake")
    Path(args["--report"]).write_text(json.dumps({
        "before": {"tris": 100, "watertight": False},
        "after": {"tris": 25, "watertight": True},
        "target_tris": 25000, "voxel_size": 0.004}))
    print("DONE in 0.1s")
    sys.exit(0)
if script.endswith("chain_blender_stage.py"):
    job = json.loads(Path(rest[0]).read_text())
    result_path = Path(job["result"])
    stage_dir = result_path.parent
    kind = job["job"]
    if kind == "convert":
        Path(job["output"]).write_bytes(b"glTF-fake")
        result = {"status": "ok", "objects": 1, "vertices": 10}
    elif kind == "export_stl":
        Path(job["output"]).write_bytes(b"solid fake\\nendsolid fake\\n")
        result = {"status": "ok", "objects": 1, "vertices": 10,
                  "scale": job.get("scale", 1.0)}
    elif kind == "orient":
        result = {"status": "ok", "object": "mesh",
                  "mode": job["options"]["mode"],
                  "input_vertices": 100, "output_vertices": 900,
                  "arrow_instances": 100}
    elif kind == "viseme":
        missing = os.environ.get("FAKE_VISEME", "full") == "missing"
        base = {"object": "mesh", "vertex_count": 100,
                "key_count": 0 if missing else 17,
                "all_keys": [],
                "visemes_present": [] if missing else ["vrc.v_aa"],
                "visemes_missing": ["vrc.v_aa"] if missing else [],
                "viseme_variants": {},
                "blink": {"ok": not missing},
                "blink_missing": [], "near_duplicates": [],
                "dead_keys": [], "pair_gaps": []}
        (stage_dir / "report.json").write_text(json.dumps(base))
        (stage_dir / "report.txt").write_text("fake viseme report\\n")
        result = {"status": "ok", "object": "mesh", "analysis": base}
        if job["options"].get("fix"):
            after = dict(base, key_count=17, visemes_missing=[],
                         visemes_present=["vrc.v_aa"],
                         blink={"ok": True})
            result["fixes"] = {"rename": {}}
            result["after"] = after
            (stage_dir / "report.json").write_text(json.dumps(after))
            if job.get("output"):
                Path(job["output"]).write_bytes(b"fake-fixed")
    else:
        result = {"status": "error", "error": "unknown job"}
    result_path.write_text(json.dumps(result))
    sys.exit(0 if result["status"] == "ok" else 3)
print("fake blender: unhandled script", script)
sys.exit(9)
'''


def write(path, text, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IEXEC)


class ChainTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="doctor-chain-test-"))
        tools = self.tmp / "tools"
        write(tools / "ripdoctor" / "ripdoctor.py", FAKE_RIPDOCTOR)
        write(tools / "ai-mesh-cleanup" / "clean_ai_mesh.py", "# fake\n")
        write(tools / "viseme-doctor" / "viseme_doctor" / "core.py",
              "# fake\n")
        write(tools / "orient-doctor" / "orient_doctor" / "core.py",
              "# fake\n")
        write(tools / "stl-doctor" / "stl_doctor.py", FAKE_STL)
        self.blender = self.tmp / "bin" / "fakeblender.py"
        write(self.blender, FAKE_BLENDER, executable=True)
        (self.tmp / "model.fbx").write_bytes(b"fake fbx")
        self.tools = {name: str(tools / dirname) for name, dirname
                      in dc.TOOL_DIR_NAMES.items()}

    def manifest(self, stages=None, **extra):
        data = {"name": "test", "input": "model.fbx",
                "blender": str(self.blender), "tools": self.tools,
                "stages": stages if stages is not None else
                [{"tool": n} for n in dc.STAGE_ORDER]}
        data.update(extra)
        path = self.tmp / "manifest.json"
        path.write_text(json.dumps(data))
        return data, path

    def run_chain(self, data, path, env=None, skip=None):
        with mock.patch.dict(os.environ, env or {}):
            return dc.run_chain(data, path, self.tmp / "out", skip)


class TestFullChain(ChainTestBase):
    def test_all_pass(self):
        stages = [{"tool": "ripdoctor"},
                  {"tool": "cleanup"},
                  {"tool": "viseme", "options": {"fix": True}},
                  {"tool": "orient"},
                  {"tool": "stl"}]
        data, path = self.manifest(stages)
        report = self.run_chain(data, path)
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual([s["status"] for s in report["stages"]],
                         ["pass"] * 5)
        out = self.tmp / "out"
        self.assertTrue((out / "chain-report.json").is_file())
        self.assertTrue((out / "chain-report.md").is_file())
        # cleanup converted the FBX to GLB first, so the viseme
        # stage's fixed export is a GLB too
        self.assertTrue(report["final_artifact"].endswith(
            "viseme-fixed.glb"))

    def test_default_stages_when_omitted(self):
        data, path = self.manifest()
        del data["stages"]
        report = self.run_chain(data, path)
        self.assertEqual([s["tool"] for s in report["stages"]],
                         dc.STAGE_ORDER)


class TestFailureStopsChain(ChainTestBase):
    def test_ripdoctor_fail(self):
        data, path = self.manifest()
        report = self.run_chain(data, path, env={"FAKE_RIP_EXIT": "1"})
        self.assertEqual(report["verdict"], "fail")
        self.assertEqual(report["failed_stage"], "ripdoctor")
        self.assertEqual(report["stages"][0]["status"], "fail")
        for s in report["stages"][1:]:
            self.assertEqual(s["status"], "skipped")
        # evidence preserved: the failing stage's own report exists
        self.assertTrue((self.tmp / "out" / "01-ripdoctor" /
                         "report.json").is_file())

    def test_cleanup_silent_death_is_error(self):
        data, path = self.manifest()
        report = self.run_chain(data, path, env={"FAKE_CLEANUP_MODE": "fail"})
        self.assertEqual(report["stages"][1]["status"], "error")
        self.assertEqual(report["verdict"], "fail")

    def test_stl_fail(self):
        data, path = self.manifest()
        report = self.run_chain(data, path, env={"FAKE_STL_EXIT": "1"})
        self.assertEqual(report["stages"][4]["status"], "fail")
        self.assertEqual(report["verdict"], "fail")


class TestSkipsAndVerdicts(ChainTestBase):
    def test_manifest_skip(self):
        stages = [{"tool": "ripdoctor"},
                  {"tool": "cleanup", "enabled": False},
                  {"tool": "viseme"}, {"tool": "orient"},
                  {"tool": "stl"}]
        data, path = self.manifest(stages)
        report = self.run_chain(data, path)
        self.assertEqual(report["stages"][1]["status"], "skipped")
        self.assertEqual(report["verdict"], "pass")

    def test_cli_skip(self):
        data, path = self.manifest()
        report = self.run_chain(data, path, skip={"cleanup", "orient"})
        statuses = {s["tool"]: s["status"] for s in report["stages"]}
        self.assertEqual(statuses["cleanup"], "skipped")
        self.assertEqual(statuses["orient"], "skipped")

    def test_viseme_warn_does_not_stop_chain(self):
        data, path = self.manifest()
        report = self.run_chain(data, path, env={"FAKE_VISEME": "missing"})
        statuses = {s["tool"]: s["status"] for s in report["stages"]}
        self.assertEqual(statuses["viseme"], "warn")
        self.assertEqual(statuses["stl"], "pass")
        self.assertEqual(report["verdict"], "pass")

    def test_viseme_strict_fails(self):
        stages = [{"tool": "ripdoctor"}, {"tool": "cleanup"},
                  {"tool": "viseme", "options": {"strict": True}},
                  {"tool": "orient"}, {"tool": "stl"}]
        data, path = self.manifest(stages)
        report = self.run_chain(data, path, env={"FAKE_VISEME": "missing"})
        self.assertEqual(report["stages"][2]["status"], "fail")
        self.assertEqual(report["stages"][3]["status"], "skipped")
        self.assertEqual(report["verdict"], "fail")


class TestManifestValidation(ChainTestBase):
    def test_unknown_tool(self):
        data, _ = self.manifest([{"tool": "nope"}])
        with self.assertRaises(dc.ManifestError):
            dc.validate_manifest(data)

    def test_missing_input_key(self):
        with self.assertRaises(dc.ManifestError):
            dc.validate_manifest({"stages": [{"tool": "stl"}]})

    def test_missing_input_file(self):
        data, path = self.manifest()
        data["input"] = "ghost.fbx"
        with self.assertRaises(dc.ManifestError):
            dc.run_chain(data, path, self.tmp / "out", None)

    def test_unresolvable_tool_dir(self):
        data, path = self.manifest()
        data["tools"] = {"ripdoctor": str(self.tmp / "nowhere")}
        with self.assertRaises(dc.ManifestError):
            self.run_chain(data, path)

    def test_yaml_manifest(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        data, _ = self.manifest()
        ypath = self.tmp / "manifest.yaml"
        ypath.write_text("name: yamltest\ninput: model.fbx\n"
                         "stages: []\n")
        loaded, _ = dc.load_manifest(ypath)
        self.assertEqual(loaded["name"], "yamltest")


if __name__ == "__main__":
    unittest.main(verbosity=2)
