"""End-to-end test: real doctors, real Blender, real assets.

Runs two full chains and cross-references every stage against the
same tool invoked standalone on the same input -- the chain's stage
report must match what the tool says on its own.

  Chain A (avatar FBX): ripdoctor -> viseme (fix) -> orient -> stl
      (cleanup skipped: voxel remesh would destroy the rig/keys --
      skipping is a manifest decision, demonstrated here)
  Chain B (generator GLB): cleanup -> viseme (analyze) -> orient
      -> stl   (ripdoctor skipped: it eats export dirs / FBX, and
      this flow has neither). Expected verdict: FAIL at stl --
      cleanup's in-scene audit says watertight/25,000 tris, but the
      GLB it exports drops 9 float32-degenerate slivers, and
      stl-doctor on the shipped file finds 24,991 tris / 27 boundary
      edges. The chain catching that cross-tool gap is the point;
      the standalone cross-references below pin the numbers.

Usage:
    python3 tests/test_e2e.py --avatar avatar.fbx \\
        --generator generator_raw.glb --blender /path/to/blender \\
        --work /tmp/doctor-chain-e2e

Assets are copied into the work dir first; originals are never
touched. Prints E2E: PASS/FAIL per check; exit 1 on any failure.
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
RELEASES = REPO.parent

FAILURES = []


def check(label, condition, detail=""):
    print(("PASS: " if condition else "FAIL: ") + label +
          (f" ({detail})" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd],
                          capture_output=True, text=True, **kw)


ORIENT_STANDALONE = '''
import json, sys
import bpy
sys.path.insert(0, sys.argv[sys.argv.index("--") + 2])
import orient_doctor.core as od

path, scale = sys.argv[sys.argv.index("--") + 1], float(
    sys.argv[sys.argv.index("--") + 3])
bpy.ops.wm.read_factory_settings(use_empty=True)
if path.lower().endswith(".fbx"):
    bpy.ops.import_scene.fbx(filepath=path)
else:
    bpy.ops.import_scene.gltf(filepath=path)
obj = max((o for o in bpy.data.objects if o.type == "MESH"),
          key=lambda o: len(o.data.vertices))
od.ensure_library()
tree = bpy.data.node_groups.new("StandaloneOrient", "GeometryNodeTree")
tree.interface.new_socket("Geometry", in_out="INPUT",
                          socket_type="NodeSocketGeometry")
tree.interface.new_socket("Geometry", in_out="OUTPUT",
                          socket_type="NodeSocketGeometry")
gin = tree.nodes.new("NodeGroupInput")
gout = tree.nodes.new("NodeGroupOutput")
viewer = tree.nodes.new("GeometryNodeGroup")
viewer.node_tree = bpy.data.node_groups["OD Viewer"]
viewer.inputs["Mode"].default_value = 2
viewer.inputs["Basis"].default_value = False
viewer.inputs["Scale"].default_value = scale
viewer.inputs["Show Base"].default_value = True
normal = tree.nodes.new("GeometryNodeInputNormal")
tree.links.new(normal.outputs["Normal"], viewer.inputs["Direction"])
realize = tree.nodes.new("GeometryNodeRealizeInstances")
tree.links.new(gin.outputs["Geometry"], viewer.inputs["Geometry"])
tree.links.new(viewer.outputs["Geometry"], realize.inputs["Geometry"])
tree.links.new(realize.outputs["Geometry"], gout.inputs["Geometry"])
mod = obj.modifiers.new("StandaloneOrient", "NODES")
mod.node_group = tree
dg = bpy.context.evaluated_depsgraph_get()
print("STANDALONE_ORIENT " + json.dumps({
    "object": obj.name,
    "input_vertices": len(obj.data.vertices),
    "output_vertices": len(obj.evaluated_get(dg).data.vertices)}))
'''


def orient_standalone(blender, artifact, orient_dir, scale):
    script = Path(artifact).parent / "orient_standalone.py"
    script.write_text(ORIENT_STANDALONE)
    proc = run([blender, "-b", "--factory-startup", "--python",
                script, "--", artifact, orient_dir, scale])
    for line in proc.stdout.splitlines():
        if line.startswith("STANDALONE_ORIENT "):
            return json.loads(line[len("STANDALONE_ORIENT "):])
    raise RuntimeError("standalone orient produced no result:\n" +
                       proc.stdout[-2000:] + proc.stderr[-2000:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--avatar", required=True)
    ap.add_argument("--generator", required=True)
    ap.add_argument("--blender", required=True)
    ap.add_argument("--work", required=True)
    args = ap.parse_args()

    work = Path(args.work)
    assets = work / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    avatar = assets / "avatar.fbx"
    generator = assets / "generator_raw.glb"
    shutil.copy2(args.avatar, avatar)
    shutil.copy2(args.generator, generator)

    def write_manifest(name, payload):
        path = work / f"{name}.json"
        payload["blender"] = args.blender
        path.write_text(json.dumps(payload, indent=1))
        return path

    avatar_manifest = write_manifest("avatar", {
        "name": "avatar-preflight", "input": str(avatar),
        "stages": [
            {"tool": "ripdoctor"},
            {"tool": "cleanup", "enabled": False},
            {"tool": "viseme", "options": {
                "fix": True, "preset": "japanese_vrc",
                "create_missing": True}},
            {"tool": "orient", "options": {
                "mode": "direction-normal", "scale": 0.05}},
            {"tool": "stl", "options": {"export_scale": 1000}},
        ]})
    generator_manifest = write_manifest("generator", {
        "name": "generator-cleanup", "input": str(generator),
        "stages": [
            {"tool": "ripdoctor", "enabled": False},
            {"tool": "cleanup", "options": {
                "target_tris": 25000, "voxel_size": 0.006}},
            {"tool": "viseme", "options": {"fix": False}},
            {"tool": "orient", "options": {
                "mode": "direction-normal", "scale": 0.05}},
            {"tool": "stl", "options": {"export_scale": 1000}},
        ]})

    avatar_out, generator_out = work / "avatar-out", work / "generator-out"
    proc_a = run([sys.executable, REPO / "doctor_chain.py",
                  avatar_manifest, "--out", avatar_out])
    print(proc_a.stdout)
    proc_b = run([sys.executable, REPO / "doctor_chain.py",
                  generator_manifest, "--out", generator_out])
    print(proc_b.stdout)
    rep_av = json.loads((avatar_out / "chain-report.json").read_text())
    rep_gen = json.loads((generator_out / "chain-report.json").read_text())
    stages_av = {s["tool"]: s for s in rep_av["stages"]}
    stages_gen = {s["tool"]: s for s in rep_gen["stages"]}

    # ---- Chain B verdict: green until stl, which fails honestly --
    check("generator cleanup pass", stages_gen["cleanup"]["status"] == "pass")
    check("generator viseme warn (no shape keys in cleaned GLB)",
          stages_gen["viseme"]["status"] == "warn")
    check("generator stl fail (GLB export drops degenerate slivers; "
          "stl-doctor catches it)",
          stages_gen["stl"]["status"] == "fail" and
          stages_gen["stl"]["metrics"]["healthy"] is False)
    check("generator chain verdict fail, stopped at stl",
          rep_gen["verdict"] == "fail" and
          rep_gen["failed_stage"] == "stl")

    # ---- Cross-reference: ripdoctor standalone vs chain (A) -------
    rip_json = work / "standalone-rip.json"
    proc = run([sys.executable, RELEASES / "ripdoctor" / "ripdoctor.py",
                avatar, "--json", rip_json])
    standalone_rip = json.loads(rip_json.read_text())
    check("ripdoctor exit matches chain status",
          (proc.returncode == 0) ==
          (stages_av["ripdoctor"]["status"] == "pass"),
          f"standalone {proc.returncode}, chain "
          f"{stages_av['ripdoctor']['status']}")
    check("ripdoctor counts match chain metrics",
          standalone_rip.get("counts") ==
          stages_av["ripdoctor"]["metrics"].get("counts"))

    # ---- Cross-reference: viseme standalone inventory (A) ---------
    viseme_report = work / "standalone-viseme.txt"
    proc = run([args.blender, "-b", "--factory-startup", "--python",
                RELEASES / "viseme-doctor" / "tests" /
                # viseme-doctor's own cross-reference script.
                "verify_real_rip.py", "--", avatar, viseme_report])
    check("viseme-doctor's own avatar verification passes standalone",
          any(line.startswith("VISEME_DOCTOR") and
          line.rstrip().endswith(": PASS")
          for line in proc.stdout.splitlines()),
          proc.stdout[-500:])
    job_result = json.loads(
        (avatar_out / "03-viseme" / "job-result.json").read_text())
    analysis = job_result["analysis"]
    check("chain viseme pre-fix analysis matches inventory "
          "(15197 verts, 44 keys, 0 visemes)",
          analysis["vertex_count"] == 15197 and
          analysis["key_count"] == 44 and
          analysis["visemes_present"] == [],
          json.dumps({k: analysis[k] for k in
                      ("vertex_count", "key_count",
                       "visemes_present")}))
    after = job_result["after"]
    check("chain viseme post-fix: all 15 visemes + blink ok",
          after["visemes_missing"] == [] and after["blink"]["ok"],
          json.dumps(after["visemes_missing"]))

    # ---- Cross-reference: cleanup standalone vs chain (B) ---------
    solo_glb = work / "standalone-cleaned.glb"
    solo_json = work / "standalone-cleanup.json"
    run([args.blender, "-b", "--factory-startup", "--python",
         RELEASES / "ai-mesh-cleanup" / "clean_ai_mesh.py", "--",
         "--input", generator, "--output", solo_glb,
         "--target-tris", "25000", "--voxel-size", "0.006",
         "--report", solo_json])
    solo = json.loads(solo_json.read_text())
    chain_cleanup = json.loads(
        (generator_out / "02-cleanup" / "report.json").read_text())
    for key in ("before", "after"):
        check(f"cleanup {key} audit matches standalone",
              solo[key] == chain_cleanup[key],
              f"solo {solo[key]} vs chain {chain_cleanup[key]}")

    # ---- Cross-reference: orient standalone vs chain (A and B) ----
    for label, stages, out in (("avatar", stages_av, avatar_out),
                               ("generator", stages_gen, generator_out)):
        stage = stages["orient"]
        solo = orient_standalone(args.blender, stage["input"],
                                 str(RELEASES / "orient-doctor"), 0.05)
        check(f"orient {label}: vertex counts match standalone",
              solo["input_vertices"] ==
              stage["metrics"]["input_vertices"] and
              solo["output_vertices"] ==
              stage["metrics"]["output_vertices"],
              f"solo {solo} vs chain {stage['metrics']}")
        check(f"orient {label}: one arrow instance per vertex",
              stage["metrics"]["arrow_instances"] ==
              stage["metrics"]["input_vertices"],
              f"arrows {stage['metrics']['arrow_instances']} vs "
              f"verts {stage['metrics']['input_vertices']}")

    # ---- Cross-reference: stl-doctor standalone vs chain ----------
    for label, stages, out in (("avatar", stages_av, avatar_out),
                               ("generator", stages_gen, generator_out)):
        stl_file = Path(out) / "05-stl" / "model.stl"
        proc = run([sys.executable,
                    RELEASES / "stl-doctor" / "stl_doctor.py",
                    "--json", stl_file])
        solo = json.loads(proc.stdout)
        check(f"stl {label}: healthy flag matches standalone",
              solo[0]["healthy"] == stages["stl"]["metrics"]["healthy"],
              f"solo {solo[0]['healthy']} vs chain "
              f"{stages['stl']['metrics']['healthy']}")
        check(f"stl {label}: triangle count matches standalone",
              solo[0]["triangles"] ==
              stages["stl"]["metrics"]["triangles"])

    print()
    print("E2E:", "FAIL (%d)" % len(FAILURES) if FAILURES else "PASS")
    print("avatar chain verdict:", rep_av["verdict"],
          "| generator chain verdict:", rep_gen["verdict"])
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
