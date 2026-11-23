#!/usr/bin/env python3
"""
chain_blender_stage.py -- the Blender-side half of doctor-chain.

doctor_chain.py (plain Python) drives this script inside Blender for
the stages that are Blender-internal by design: viseme-doctor and
orient-doctor never read or write file formats themselves, so the
chain routes through Blender -- import -> doctor -> export -- exactly
as their READMEs prescribe. This runner also does the chain's format
conversions (anything -> GLB for ai-mesh-cleanup, anything -> STL for
stl-doctor).

    blender -b --factory-startup --python chain_blender_stage.py -- job.json

The job JSON names the job type, input/output paths, tool dirs and
options. A result JSON is written to job["result"]:
{"status": "ok", ...} or {"status": "error", "error": "<traceback>"}.
Exit code 0 on ok, 3 on error (Blender itself exits 0 on script
exceptions, so the result file is the contract).

Jobs: viseme | orient | convert | export_stl

Copyright (c) 2026 1915rising -- MIT License (see LICENSE). This file
only *invokes* the doctor packages; orient-doctor remains GPL and is
loaded from its own checkout, never copied here.
"""

import json
import sys
import traceback
from pathlib import Path

import bpy


# --------------------------------------------------------------------------
# Scene helpers
# --------------------------------------------------------------------------

def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def import_model(path):
    ext = Path(path).suffix.lower()
    if ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=path)
    else:
        raise ValueError(f"unsupported model format: {path}")


def export_model(path):
    ext = Path(path).suffix.lower()
    if ext == ".fbx":
        bpy.ops.export_scene.fbx(filepath=path, use_selection=False)
    elif ext in (".glb", ".gltf"):
        bpy.ops.export_scene.gltf(filepath=path, export_format="GLB")
    else:
        raise ValueError(f"unsupported export format: {path}")


def pick_object(name=None, prefer_shape_keys=False):
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    if not meshes:
        return None
    if name:
        obj = bpy.data.objects.get(name)
        if obj is not None:
            return obj
    if prefer_shape_keys:
        def key_count(o):
            return len(o.data.shape_keys.key_blocks) \
                if o.data.shape_keys else 0
        best = max(meshes, key=key_count)
        if key_count(best) > 0:
            return best
    return max(meshes, key=lambda o: len(o.data.vertices))


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

def job_viseme(job):
    sys.path.insert(0, job["viseme_dir"])
    from viseme_doctor import core as vd

    opts = job.get("options") or {}
    import_model(job["input"])
    obj = pick_object(opts.get("object"), prefer_shape_keys=True)
    if obj is None:
        raise RuntimeError("no mesh object in input")
    out = {"object": obj.name, "analysis": vd.analyze(obj)}
    stage_dir = Path(job["result"]).parent
    (stage_dir / "report.txt").write_text(
        vd.format_report(obj, out["analysis"]) + "\n", encoding="utf-8")

    if opts.get("fix"):
        fixes = {}
        if opts.get("mapping"):
            mapping = vd.load_mapping(opts["mapping"])
        else:
            mapping = vd.PRESETS[opts.get("preset", "japanese_vrc")]
        fixes["rename"] = vd.apply_rename(obj, mapping)
        if opts.get("create_missing"):
            fixes["create_missing"] = vd.create_missing(obj)
        mirrors = []
        for pair in opts.get("mirror") or []:
            src, tgt = pair[0], pair[1]
            mirrors.append({src: vd.mirror_key(obj, src, tgt)})
        if mirrors:
            fixes["mirror"] = mirrors
        out["fixes"] = fixes
        out["after"] = vd.analyze(obj)
        (stage_dir / "report.txt").write_text(
            vd.format_report(obj, out["after"]) + "\n",
            encoding="utf-8")

    final = out.get("after") or out["analysis"]
    (stage_dir / "report.json").write_text(
        json.dumps(final, indent=1, ensure_ascii=False),
        encoding="utf-8")
    if job.get("output"):
        # Export the whole scene so armature + sibling meshes survive.
        export_model(job["output"])
    return out


def job_orient(job):
    sys.path.insert(0, job["orient_dir"])
    import orient_doctor.core as od

    opts = job.get("options") or {}
    import_model(job["input"])
    obj = pick_object(opts.get("object"))
    if obj is None:
        raise RuntimeError("no mesh object in input")
    od.ensure_library()
    group = bpy.data.node_groups["OD Viewer"]

    tree = bpy.data.node_groups.new("ChainOrientCheck", "GeometryNodeTree")
    tree.interface.new_socket("Geometry", in_out="INPUT",
                              socket_type="NodeSocketGeometry")
    tree.interface.new_socket("Geometry", in_out="OUTPUT",
                              socket_type="NodeSocketGeometry")
    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    viewer = tree.nodes.new("GeometryNodeGroup")
    viewer.node_tree = group
    mode = opts.get("mode", "direction-normal")
    viewer.inputs["Mode"].default_value = 2  # Direction
    viewer.inputs["Basis"].default_value = bool(opts.get("basis"))
    viewer.inputs["Scale"].default_value = float(opts.get("scale", 0.1))
    viewer.inputs["Show Base"].default_value = True
    if mode == "direction-normal":
        src = tree.nodes.new("GeometryNodeInputNormal")
        tree.links.new(src.outputs["Normal"], viewer.inputs["Direction"])
    elif mode == "direction-attribute":
        src = tree.nodes.new("GeometryNodeInputNamedAttribute")
        src.data_type = "FLOAT_VECTOR"
        src.inputs["Name"].default_value = opts.get("attribute", "")
        tree.links.new(src.outputs["Attribute"],
                       viewer.inputs["Direction"])
    else:
        raise ValueError(f"unknown orient mode: {mode}")
    realize = tree.nodes.new("GeometryNodeRealizeInstances")
    tree.links.new(gin.outputs["Geometry"], viewer.inputs["Geometry"])
    tree.links.new(viewer.outputs["Geometry"],
                   gout.inputs["Geometry"])
    mod = obj.modifiers.new("ChainOrientCheck", "NODES")
    mod.node_group = tree

    input_verts = len(obj.data.vertices)
    # First evaluation, instances still instanced: count the arrows.
    dg = bpy.context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(dg)
    instances = None
    try:
        instances = sum(1 for inst in dg.object_instances
                        if inst.is_instance and
                        inst.parent == eval_obj)
    except Exception:
        instances = None
    # Second evaluation, instances realized: count the real geometry.
    tree.links.new(viewer.outputs["Geometry"],
                   realize.inputs["Geometry"])
    tree.links.new(realize.outputs["Geometry"],
                   gout.inputs["Geometry"])
    dg = bpy.context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(dg)
    output_verts = len(eval_obj.data.vertices)
    return {"object": obj.name, "mode": mode,
            "input_vertices": input_verts,
            "output_vertices": output_verts,
            "arrow_instances": instances}


def job_convert(job):
    import_model(job["input"])
    export_model(job["output"])
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    return {"objects": len(meshes),
            "vertices": sum(len(o.data.vertices) for o in meshes)}


def job_export_stl(job):
    import_model(job["input"])
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    if not meshes:
        raise RuntimeError("no mesh object in input")
    bpy.ops.object.select_all(action="DESELECT")
    for o in meshes:
        o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    scale = float(job.get("scale", 1.0))
    props = bpy.ops.wm.stl_export.get_rna_type().properties
    kwargs = {"filepath": job["output"]}
    if "global_scale" in props:
        kwargs["global_scale"] = scale
    if "use_selection" in props:
        kwargs["use_selection"] = False
    bpy.ops.wm.stl_export(**kwargs)
    return {"objects": len(meshes),
            "vertices": sum(len(o.data.vertices) for o in meshes),
            "scale": scale}


JOBS = {"viseme": job_viseme, "orient": job_orient,
        "convert": job_convert, "export_stl": job_export_stl}


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    job = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    result_path = Path(job["result"])
    try:
        reset_scene()
        data = JOBS[job["job"]](job)
        result = {"status": "ok"}
        result.update(data)
        code = 0
    except Exception:
        result = {"status": "error", "error": traceback.format_exc()}
        code = 3
    result_path.write_text(json.dumps(result, indent=1,
                                      ensure_ascii=False, default=str),
                           encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
