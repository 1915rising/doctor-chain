#!/usr/bin/env python3
"""
doctor_chain.py -- run the 1915rising doctor tools as one preflight.

    ripdoctor -> ai-mesh-cleanup -> viseme-doctor -> orient-doctor
    -> stl-doctor

One manifest (JSON, or YAML when PyYAML is installed) describes a run:
the input model, which stages are on, and each stage's options. The
driver invokes every tool as a *separate process* -- the tools are
aggregated, never combined -- captures each stage's exit code and
JSON report into one combined chain report, and stops at the first
failing stage with that stage's evidence preserved on disk.

Why separate processes: four of the doctors are MIT and orient-doctor
is GPL-3.0-or-later. Mere aggregation (an orchestrator invoking
independent programs) keeps every tool under its own license. No tool
code is copied into this repo, and none ever should be.

Usage:
    python3 doctor_chain.py manifest.json [--out DIR] [--skip a,b]

Exit codes: 0 = chain passed (warnings allowed), 1 = a stage failed
or errored, 2 = usage / manifest error.

Copyright (c) 2026 1915rising -- MIT License (see LICENSE).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Canonical stage order and the repo directory each stage's tool
# lives in (used for sibling-checkout resolution).
STAGE_ORDER = ["ripdoctor", "cleanup", "viseme", "orient", "stl"]
TOOL_DIR_NAMES = {
    "ripdoctor": "ripdoctor",
    "cleanup": "ai-mesh-cleanup",
    "viseme": "viseme-doctor",
    "orient": "orient-doctor",
    "stl": "stl-doctor",
}
# Entry point inside each tool dir, per stage.
TOOL_ENTRY = {
    "ripdoctor": "ripdoctor.py",
    "cleanup": "clean_ai_mesh.py",
    "viseme": "viseme_doctor/core.py",      # existence probe only
    "orient": "orient_doctor/core.py",      # existence probe only
    "stl": "stl_doctor.py",
}

DEFAULT_STAGE_TIMEOUT = 3600  # seconds; Blender stages on big soup are slow


# --------------------------------------------------------------------------
# Manifest loading / validation
# --------------------------------------------------------------------------

class ManifestError(Exception):
    pass


def load_manifest(path):
    path = Path(path)
    if not path.is_file():
        raise ManifestError(f"manifest not found: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            raise ManifestError(
                "YAML manifest given but PyYAML is not installed; "
                "use a JSON manifest or `pip install pyyaml`")
        data = yaml.safe_load(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"manifest is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON/YAML object")
    return data, path


def validate_manifest(data):
    if "input" not in data:
        raise ManifestError("manifest needs an 'input' (model file or "
                            "export directory)")
    stages = data.get("stages")
    if stages is None:
        # No stage list: run the full canonical chain, all defaults.
        stages = [{"tool": name} for name in STAGE_ORDER]
        data["stages"] = stages
    if not isinstance(stages, list) or not stages:
        raise ManifestError("'stages' must be a non-empty list")
    for i, stage in enumerate(stages):
        if not isinstance(stage, dict) or "tool" not in stage:
            raise ManifestError(f"stage #{i + 1} has no 'tool' name")
        if stage["tool"] not in STAGE_ORDER:
            raise ManifestError(
                f"stage #{i + 1}: unknown tool {stage['tool']!r} "
                f"(known: {', '.join(STAGE_ORDER)})")
        if "options" in stage and not isinstance(stage["options"], dict):
            raise ManifestError(f"stage {stage['tool']!r}: 'options' "
                                "must be an object")
    return data


# --------------------------------------------------------------------------
# Tool / Blender resolution
# --------------------------------------------------------------------------

def resolve_tool_dir(tool, data):
    """Find a doctor's checkout directory.

    Order: an explicit manifest tools.<tool> or
    $DOCTOR_CHAIN_<TOOL>_DIR is authoritative (a bad explicit path is
    an error, never a silent fallback -- that would mask typos).
    Otherwise: manifest tools_dir/<dirname> -> sibling of this repo
    (the github-releases layout, or any folder where the repos are
    cloned side by side) -> sibling of this script.
    """
    dirname = TOOL_DIR_NAMES[tool]
    tools_map = data.get("tools") or {}
    explicit = tools_map.get(tool) or \
        os.environ.get(f"DOCTOR_CHAIN_{tool.upper()}_DIR")
    if explicit:
        cand = Path(explicit).expanduser()
        if (cand / TOOL_ENTRY[tool]).is_file():
            return cand.resolve()
        raise ManifestError(
            f"manifest/env points {tool} at {cand}, but "
            f"{TOOL_ENTRY[tool]} is not there")
    candidates = []
    if data.get("tools_dir"):
        candidates.append(Path(data["tools_dir"]).expanduser() / dirname)
    candidates.append(HERE.parent / dirname)
    candidates.append(HERE / dirname)
    for cand in candidates:
        if (cand / TOOL_ENTRY[tool]).is_file():
            return cand.resolve()
    searched = ", ".join(str(c) for c in candidates)
    raise ManifestError(
        f"cannot find the {tool} tool (looked for {TOOL_ENTRY[tool]} "
        f"in: {searched}); set tools.{tool} in the manifest")


def resolve_blender(data):
    cand = data.get("blender") or os.environ.get("DOCTOR_CHAIN_BLENDER")
    if cand:
        if Path(cand).is_file() or shutil.which(cand):
            return cand
        raise ManifestError(f"blender path from manifest/env not "
                            f"usable: {cand}")
    found = shutil.which("blender")
    if found:
        return found
    raise ManifestError("Blender not found on PATH; set 'blender' in "
                        "the manifest or $DOCTOR_CHAIN_BLENDER")


# --------------------------------------------------------------------------
# Subprocess plumbing
# --------------------------------------------------------------------------

def run_cmd(cmd, stage_dir, timeout):
    """Run a command, tee stdout/stderr to files in the stage dir."""
    started = time.time()
    stdout_path = stage_dir / "stdout.log"
    stderr_path = stage_dir / "stderr.log"
    try:
        with open(stdout_path, "w", encoding="utf-8") as out, \
                open(stderr_path, "w", encoding="utf-8") as err:
            proc = subprocess.run([str(c) for c in cmd], stdout=out,
                                  stderr=err, timeout=timeout)
        return proc.returncode, round(time.time() - started, 1)
    except subprocess.TimeoutExpired:
        return None, round(time.time() - started, 1)
    except OSError as exc:
        with open(stderr_path, "a", encoding="utf-8") as err:
            err.write(f"\n[doctor-chain] failed to launch: {exc}\n")
        return None, round(time.time() - started, 1)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------
# Stage result
# --------------------------------------------------------------------------

class StageResult:
    def __init__(self, tool):
        self.tool = tool
        self.status = "error"          # pass | warn | fail | error | skipped
        self.exit_code = None
        self.seconds = 0.0
        self.input_artifact = None
        self.output_artifact = None
        self.metrics = {}
        self.notes = []
        self.reports = {}              # label -> path (relative to out dir)

    def to_dict(self):
        return {
            "tool": self.tool,
            "status": self.status,
            "exit_code": self.exit_code,
            "seconds": self.seconds,
            "input": self.input_artifact,
            "output": self.output_artifact,
            "metrics": self.metrics,
            "notes": self.notes,
            "reports": self.reports,
        }


class Context:
    def __init__(self, data, manifest_path, out_dir):
        self.data = data
        self.manifest_path = manifest_path
        self.out_dir = out_dir
        base = manifest_path.resolve().parent
        raw_input = Path(data["input"]).expanduser()
        self.input_path = (raw_input if raw_input.is_absolute()
                           else (base / raw_input)).resolve()
        self.current = self.input_path   # artifact flowing through stages
        self.blender = None              # resolved lazily (not every
                                         # chain needs Blender)
        self._tool_dirs = {}

    def tool_dir(self, tool):
        if tool not in self._tool_dirs:
            self._tool_dirs[tool] = resolve_tool_dir(tool, self.data)
        return self._tool_dirs[tool]

    def blender_bin(self):
        if self.blender is None:
            self.blender = resolve_blender(self.data)
        return self.blender

    def rel(self, path):
        try:
            return str(Path(path).relative_to(self.out_dir))
        except (ValueError, TypeError):
            return str(path)


# --------------------------------------------------------------------------
# Blender runner helper (viseme / orient / convert / export_stl jobs)
# --------------------------------------------------------------------------

def blender_job(ctx, stage_dir, job):
    """Run chain_blender_stage.py for one job dict; return its result
    dict (or None) plus (exit_code, seconds)."""
    job = dict(job)
    result_path = stage_dir / "job-result.json"
    job["result"] = str(result_path)
    job_path = stage_dir / "job.json"
    job_path.write_text(json.dumps(job, indent=1), encoding="utf-8")
    cmd = [ctx.blender_bin(), "-b", "--factory-startup",
           "--python", str(HERE / "chain_blender_stage.py"),
           "--", str(job_path)]
    timeout = job.get("timeout", DEFAULT_STAGE_TIMEOUT)
    code, secs = run_cmd(cmd, stage_dir, timeout)
    return read_json(result_path), code, secs


# --------------------------------------------------------------------------
# Stage adapters -- each returns a StageResult. The artifact that flows
# to the next stage is ctx.current, updated by producing stages.
# --------------------------------------------------------------------------

def stage_ripdoctor(ctx, stage, stage_dir):
    res = StageResult("ripdoctor")
    tool_dir = ctx.tool_dir("ripdoctor")
    options = stage.get("options") or {}
    target_raw = options.get("target") or str(ctx.current)
    target = Path(target_raw).expanduser()
    if not target.is_absolute():
        target = (ctx.manifest_path.resolve().parent / target).resolve()
    res.input_artifact = str(target)
    if not target.exists():
        res.notes.append(f"ripdoctor target does not exist: {target}")
        return res
    if target.is_file() and target.suffix.lower() != ".fbx":
        res.notes.append(
            "ripdoctor takes an AssetRipper export directory or a "
            f".fbx file; got {target.name}. Point options.target at "
            "one of those, or disable this stage for this input.")
        return res
    report_json = stage_dir / "report.json"
    report_md = stage_dir / "report.md"
    cmd = [sys.executable, str(tool_dir / "ripdoctor.py"), str(target),
           "--json", str(report_json), "--md", str(report_md)]
    if options.get("fbx_probe") and target.is_dir():
        cmd.append("--fbx")
    code, secs = run_cmd(cmd, stage_dir,
                         stage.get("timeout", DEFAULT_STAGE_TIMEOUT))
    res.exit_code, res.seconds = code, secs
    res.reports = {"json": ctx.rel(report_json), "md": ctx.rel(report_md)}
    data = read_json(report_json)
    if data:
        res.metrics = {"kind": data.get("kind"),
                       "counts": data.get("counts")}
    if code == 0:
        res.status = "pass"
    elif code == 1:
        res.status = "fail"
        res.notes.append("ripdoctor verdict: FAIL (see report.md)")
    elif code is None:
        res.notes.append("ripdoctor did not finish (timeout/launch "
                         "failure; see stderr.log)")
    else:
        res.notes.append(f"ripdoctor exited {code} (usage error)")
    return res


def stage_cleanup(ctx, stage, stage_dir):
    res = StageResult("cleanup")
    tool_dir = ctx.tool_dir("cleanup")
    options = stage.get("options") or {}
    source = ctx.current
    res.input_artifact = str(source)
    if source.suffix.lower() not in (".glb", ".gltf"):
        # ai-mesh-cleanup eats GLB only; convert through Blender first.
        conv_dir = stage_dir / "convert"
        conv_dir.mkdir(exist_ok=True)
        converted = conv_dir / (source.stem + ".glb")
        result, code, _ = blender_job(ctx, conv_dir, {
            "job": "convert", "input": str(source),
            "output": str(converted)})
        if not result or result.get("status") != "ok":
            res.notes.append(
                "input is not a .glb/.gltf and the Blender conversion "
                "to GLB failed (see convert/stdout.log)")
            res.exit_code = code
            return res
        res.notes.append(f"converted {source.suffix} -> GLB for "
                         "cleanup (cleanup is GLB-only)")
        source = converted
    out_glb = stage_dir / "cleaned.glb"
    report_json = stage_dir / "report.json"
    cmd = [ctx.blender_bin(), "-b", "--factory-startup",
           "--python", str(tool_dir / "clean_ai_mesh.py"), "--",
           "--input", str(source), "--output", str(out_glb),
           "--target-tris", str(options.get("target_tris", 25000)),
           "--voxel-size", str(options.get("voxel_size", 0.004)),
           "--report", str(report_json)]
    code, secs = run_cmd(cmd, stage_dir,
                         stage.get("timeout", DEFAULT_STAGE_TIMEOUT))
    res.exit_code, res.seconds = code, secs
    res.reports = {"json": ctx.rel(report_json)}
    report = read_json(report_json)
    # Blender exits 0 even when a --python script throws, so the
    # report + output file are the real success signal.
    if not out_glb.is_file() or not report or "after" not in report:
        res.status = "error"
        res.notes.append("cleanup produced no output/report "
                         "(see stdout.log / stderr.log)")
        return res
    res.metrics = {"before": report.get("before"),
                   "after": report.get("after"),
                   "target_tris": report.get("target_tris"),
                   "voxel_size": report.get("voxel_size")}
    res.output_artifact = str(out_glb)
    ctx.current = out_glb
    if report["after"].get("watertight"):
        res.status = "pass"
    else:
        res.status = "fail"
        res.notes.append("cleaned mesh is not watertight after "
                         "voxel remesh (see report.json)")
    return res


def _viseme_verdict(analysis, options):
    """Map a viseme-doctor analysis dict to (status, notes)."""
    notes = []
    missing = analysis.get("visemes_missing") or []
    blink = analysis.get("blink") or {}
    if missing:
        notes.append(f"{len(missing)} visemes missing under standard "
                     f"names: {', '.join(missing)}")
    if not blink.get("ok"):
        notes.append("blink not satisfied (need `blink` or the "
                     "blink.L/blink.R pair)")
    if analysis.get("key_count", 0) == 0:
        notes.append("mesh carries no shape keys at all")
    if not notes:
        return "pass", notes
    if options.get("strict"):
        return "fail", notes
    return "warn", notes


def stage_viseme(ctx, stage, stage_dir):
    res = StageResult("viseme")
    options = stage.get("options") or {}
    res.input_artifact = str(ctx.current)
    fix = bool(options.get("fix"))
    export_path = None
    if fix:
        fmt = (options.get("export") or
               ctx.current.suffix.lower().lstrip(".") or "glb")
        if fmt not in ("fbx", "glb"):
            res.notes.append(f"viseme export format must be fbx|glb, "
                             f"got {fmt!r}")
            return res
        export_path = stage_dir / f"viseme-fixed.{fmt}"
    job = {
        "job": "viseme",
        "input": str(ctx.current),
        "output": str(export_path) if export_path else None,
        "viseme_dir": str(ctx.tool_dir("viseme")),
        "options": {
            "object": options.get("object"),
            "fix": fix,
            "preset": options.get("preset", "japanese_vrc"),
            "mapping": options.get("mapping"),
            "create_missing": bool(options.get("create_missing")),
            "mirror": options.get("mirror") or [],
        },
    }
    result, code, secs = blender_job(ctx, stage_dir, job)
    res.exit_code, res.seconds = code, secs
    res.reports = {"json": ctx.rel(stage_dir / "report.json"),
                   "txt": ctx.rel(stage_dir / "report.txt")}
    if not result or result.get("status") != "ok":
        res.notes.append("viseme stage errored in Blender "
                         "(see stdout.log / stderr.log)")
        if result and result.get("error"):
            res.notes.append(result["error"].splitlines()[-1])
        return res
    final = result.get("after") or result.get("analysis") or {}
    res.metrics = {
        "object": result.get("object"),
        "vertex_count": final.get("vertex_count"),
        "key_count": final.get("key_count"),
        "visemes_present": len(final.get("visemes_present") or []),
        "visemes_missing": len(final.get("visemes_missing") or []),
        "blink_ok": (final.get("blink") or {}).get("ok"),
        "dead_keys": len(final.get("dead_keys") or []),
        "pair_gaps": len(final.get("pair_gaps") or []),
        "fixes": result.get("fixes"),
    }
    status, notes = _viseme_verdict(final, options)
    res.status = status
    res.notes.extend(notes)
    if export_path and export_path.is_file():
        res.output_artifact = str(export_path)
        ctx.current = export_path
    elif fix:
        res.status = "error"
        res.notes.append("fixes ran but no export was produced")
    return res


def stage_orient(ctx, stage, stage_dir):
    res = StageResult("orient")
    options = stage.get("options") or {}
    res.input_artifact = str(ctx.current)
    job = {
        "job": "orient",
        "input": str(ctx.current),
        "orient_dir": str(ctx.tool_dir("orient")),
        "options": {
            "object": options.get("object"),
            "mode": options.get("mode", "direction-normal"),
            "attribute": options.get("attribute"),
            "basis": bool(options.get("basis")),
            "scale": float(options.get("scale", 0.1)),
        },
    }
    result, code, secs = blender_job(ctx, stage_dir, job)
    res.exit_code, res.seconds = code, secs
    res.reports = {"json": ctx.rel(stage_dir / "report.json")}
    if not result or result.get("status") != "ok":
        res.notes.append("orient stage errored in Blender "
                         "(see stdout.log / stderr.log)")
        if result and result.get("error"):
            res.notes.append(result["error"].splitlines()[-1])
        return res
    res.metrics = {
        "object": result.get("object"),
        "mode": result.get("mode"),
        "input_vertices": result.get("input_vertices"),
        "output_vertices": result.get("output_vertices"),
        "arrow_instances": result.get("arrow_instances"),
    }
    if (result.get("output_vertices") or 0) > \
            (result.get("input_vertices") or 0):
        res.status = "pass"
    else:
        res.status = "fail"
        res.notes.append("OD Viewer produced no arrow geometry on "
                         "this model (see report.json)")
    return res


def stage_stl(ctx, stage, stage_dir):
    res = StageResult("stl")
    tool_dir = ctx.tool_dir("stl")
    options = stage.get("options") or {}
    res.input_artifact = str(ctx.current)
    stl_path = stage_dir / "model.stl"
    exp_dir = stage_dir / "export"
    exp_dir.mkdir(exist_ok=True)
    result, code, _ = blender_job(ctx, exp_dir, {
        "job": "export_stl", "input": str(ctx.current),
        "output": str(stl_path),
        "scale": float(options.get("export_scale", 1.0))})
    if not result or result.get("status") != "ok" or \
            not stl_path.is_file():
        res.notes.append("STL export failed in Blender "
                         "(see export/stdout.log)")
        res.exit_code = code
        return res
    res.reports = {"stl": ctx.rel(stl_path),
                   "json": ctx.rel(stage_dir / "report.json")}
    report_json = stage_dir / "report.json"
    cmd = [sys.executable, str(tool_dir / "stl_doctor.py"),
           "--json", "-o", str(report_json), str(stl_path)]
    code, secs = run_cmd(cmd, stage_dir,
                         stage.get("timeout", DEFAULT_STAGE_TIMEOUT))
    res.exit_code, res.seconds = code, secs
    files = read_json(report_json) or []
    if files:
        first = files[0]
        res.metrics = {
            "triangles": first.get("triangles"),
            "components": first.get("components"),
            "watertight": first.get("watertight"),
            "boundary_edges": first.get("boundary_edges"),
            "nonmanifold_edges": first.get("nonmanifold_edges"),
            "degenerate_faces": first.get("degenerate_faces"),
            "healthy": first.get("healthy"),
            "problems": first.get("problems"),
        }
    if code == 0:
        res.status = "pass"
    elif code == 1:
        res.status = "fail"
        res.notes.append("stl-doctor verdict: FAIL (see report.json "
                         "problems)")
    elif code is None:
        res.notes.append("stl-doctor did not finish (see stderr.log)")
    else:
        res.notes.append(f"stl-doctor exited {code} (usage/load "
                         "error -- it needs trimesh + NumPy in this "
                         "Python; see stderr.log)")
    return res


ADAPTERS = {
    "ripdoctor": stage_ripdoctor,
    "cleanup": stage_cleanup,
    "viseme": stage_viseme,
    "orient": stage_orient,
    "stl": stage_stl,
}


# --------------------------------------------------------------------------
# Combined report
# --------------------------------------------------------------------------

def render_md(report):
    lines = [f"# Doctor Chain report — {report['name']}", ""]
    lines.append(f"- Input: `{report['input']}`")
    lines.append(f"- Chain verdict: **{report['verdict'].upper()}**")
    if report.get("failed_stage"):
        lines.append(f"- Stopped at stage: **{report['failed_stage']}**")
    lines.append(f"- Started: {report['started']} · "
                 f"total {report['seconds']}s")
    lines.append("")
    lines.append("| # | Stage | Status | Time | Verdict detail |")
    lines.append("|---|---|---|---|---|")
    for i, s in enumerate(report["stages"], 1):
        detail = "; ".join(s["notes"]) or _metric_summary(s)
        lines.append(f"| {i} | {s['tool']} | {s['status']} | "
                     f"{s['seconds']}s | {detail} |")
    lines.append("")
    for i, s in enumerate(report["stages"], 1):
        lines.append(f"## Stage {i} — {s['tool']} ({s['status']})")
        if s["input"]:
            lines.append(f"- in: `{s['input']}`")
        if s["output"]:
            lines.append(f"- out: `{s['output']}`")
        for label, rel in s["reports"].items():
            lines.append(f"- report ({label}): `{rel}`")
        for note in s["notes"]:
            lines.append(f"- note: {note}")
        lines.append("")
    lines.append("Reports per stage live beside this file; a failed "
                 "stage's stdout/stderr and JSON are its evidence.")
    return "\n".join(lines) + "\n"


def _metric_summary(stage):
    m = stage.get("metrics") or {}
    bits = []
    for key in ("visemes_present", "visemes_missing", "key_count",
                "arrow_instances", "triangles", "watertight",
                "healthy"):
        if key in m and m[key] is not None:
            bits.append(f"{key}={m[key]}")
    return ", ".join(bits)


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def run_chain(data, manifest_path, out_dir, skip_cli):
    validate_manifest(data)
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    ctx = Context(data, manifest_path, out_dir)
    if not ctx.input_path.exists():
        raise ManifestError(f"input does not exist: {ctx.input_path}")
    report = {
        "name": data.get("name", "doctor-chain"),
        "input": str(ctx.input_path),
        "started": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "stages": [],
        "verdict": "pass",
        "failed_stage": None,
    }
    t0 = time.time()
    skip = {s.strip() for s in skip_cli} if skip_cli else set()
    stopped = False
    for idx, stage in enumerate(data["stages"], 1):
        tool = stage["tool"]
        entry = {"tool": tool, "status": "skipped", "exit_code": None,
                 "seconds": 0.0, "input": None, "output": None,
                 "metrics": {}, "notes": [], "reports": {}}
        if stopped:
            entry["notes"].append("not run: chain stopped at an "
                                  "earlier stage")
            report["stages"].append(entry)
            continue
        if stage.get("enabled") is False or stage.get("skip") or \
                tool in skip:
            entry["notes"].append("skipped in manifest / by --skip")
            report["stages"].append(entry)
            continue
        stage_dir = out_dir / f"{idx:02d}-{tool}"
        stage_dir.mkdir(exist_ok=True)
        try:
            res = ADAPTERS[tool](ctx, stage, stage_dir)
        except ManifestError:
            raise
        except Exception as exc:  # adapter bug: keep evidence, stop
            res = StageResult(tool)
            res.notes.append(f"adapter exception: {exc!r}")
        report["stages"].append(res.to_dict())
        if res.status in ("fail", "error"):
            report["verdict"] = "fail"
            report["failed_stage"] = tool
            stopped = True
    report["seconds"] = round(time.time() - t0, 1)
    report["final_artifact"] = str(ctx.current)
    (out_dir / "chain-report.json").write_text(
        json.dumps(report, indent=1), encoding="utf-8")
    (out_dir / "chain-report.md").write_text(
        render_md(report), encoding="utf-8")
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="doctor-chain",
        description="Run the 1915rising doctor tools as one "
                    "preflight chain (see README).")
    ap.add_argument("manifest", help="chain manifest (.json, or "
                                     ".yaml with PyYAML installed)")
    ap.add_argument("--out", help="output directory (default: "
                                  "<manifest dir>/<name>-chain-out)")
    ap.add_argument("--skip", help="comma-separated stage names to "
                                   "skip for this run")
    args = ap.parse_args(argv)
    try:
        data, manifest_path = load_manifest(args.manifest)
        validate_manifest(data)
        out_dir = args.out or str(
            manifest_path.resolve().parent /
            f"{data.get('name', 'doctor-chain')}-chain-out")
        skip = args.skip.split(",") if args.skip else None
        report = run_chain(data, manifest_path, out_dir, skip)
    except ManifestError as exc:
        print(f"doctor-chain: {exc}", file=sys.stderr)
        return 2
    print(f"chain verdict: {report['verdict'].upper()}"
          + (f" (stopped at {report['failed_stage']})"
             if report["failed_stage"] else "")
          + f" — report: {out_dir}/chain-report.md")
    for s in report["stages"]:
        print(f"  {s['tool']:<10} {s['status']}")
    return 0 if report["verdict"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
