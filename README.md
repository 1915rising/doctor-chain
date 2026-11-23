# Doctor Chain

One preflight for the whole doctor family:

```
ripdoctor → ai-mesh-cleanup → viseme-doctor → orient-doctor → stl-doctor
```

Model in, verified model out, **one combined report** — instead of
five tools invoked separately, in the right order, by a human who
remembers the order.

Each doctor already speaks exit codes and JSON. What was missing is
the conductor: a **manifest** that says which stages run and with
what options, and a **driver** that runs them in order, threads the
working artifact from stage to stage, captures every stage's own
report into a single chain report, and **stops at the first failing
stage with that stage's evidence preserved** (its report, stdout and
stderr stay on disk, in its own folder).

## The doctors

| Stage | Tool | What it gates |
|---|---|---|
| 1 | [ripdoctor](https://github.com/1915rising/ripdoctor) | AssetRipper exports and FBX: dummy shaders, material census, scale plausibility |
| 2 | [ai-mesh-cleanup](https://github.com/1915rising/ai-mesh-cleanup) | AI-generator GLB soup → one watertight solid at a chosen triangle count |
| 3 | [viseme-doctor](https://github.com/1915rising/viseme-doctor) | VRChat viseme/blink shape-key names: verify, bulk rename, create, mirror |
| 4 | [orient-doctor](https://github.com/1915rising/orient-doctor) | Geometry Nodes orientation data, drawn as real, measurable arrow geometry |
| 5 | [stl-doctor](https://github.com/1915rising/stl-doctor) | The final STL: watertight? components? degenerate faces? printable? |

Doctor Chain contains **none of their code**. It finds their
checkouts on disk and invokes them.

## Why separate processes (the licensing shape)

Four doctors are MIT; orient-doctor is GPL-3.0-or-later (it is a
port of GPL work). A single bundle that *combined* them into one
work would drag the bundle under the GPL. Doctor Chain is an
**orchestrator invoking independent programs** — mere aggregation:
every tool keeps its own license, and Doctor Chain itself is MIT.
That is a design constraint, not an accident; don't "fix" it by
vendoring a doctor into this repo.

## Requirements

- Python 3.8+ for the driver (standard library only; PyYAML only if
  you write manifests in YAML).
- The doctor checkouts you want to run, cloned anywhere — side by
  side with this repo is the zero-config layout.
- Blender 4.2 LTS on your `PATH` (or named in the manifest) for the
  cleanup / viseme / orient / stl-export stages.
- stl-doctor's own requirements (trimesh + NumPy) in the Python you
  run the driver with.

## The manifest

```json
{
  "name": "avatar-preflight",
  "input": "avatar.fbx",
  "blender": "blender",
  "stages": [
    {"tool": "ripdoctor"},
    {"tool": "cleanup", "enabled": false},
    {"tool": "viseme", "options": {"fix": true,
             "preset": "japanese_vrc", "create_missing": true}},
    {"tool": "orient", "options": {"mode": "direction-normal",
             "scale": 0.05}},
    {"tool": "stl", "options": {"export_scale": 1000}}
  ]
}
```

- `input` — the model (or, for a ripdoctor-first chain, the export /
  FBX it should eat). Relative paths resolve against the manifest.
- `stages` — run in listed order; omit the list entirely to run the
  full canonical chain with defaults. Any stage can be turned off
  with `"enabled": false` (cleanup above: voxel remeshing a rigged
  avatar would destroy its rig and shape keys — skipping is a
  decision the manifest records, not an oversight).
- `tools` — optional map of stage name → checkout path, when the
  doctors don't live beside this repo. `$DOCTOR_CHAIN_<TOOL>_DIR`
  does the same job. An explicit path that is wrong is an error,
  never a silent fallback.
- `blender` — optional Blender executable (`$DOCTOR_CHAIN_BLENDER`
  also works).

### Stage options that matter

| Stage | Options |
|---|---|
| ripdoctor | `target` (defaults to the flowing artifact), `fbx_probe` (probe FBXs inside an export dir) |
| cleanup | `target_tris` (25000), `voxel_size` (0.004 — see ai-mesh-cleanup's 0.3–0.4 %-of-height rule). Non-GLB input is converted through Blender first |
| viseme | `fix` (apply fixes + export, default off = analyze only), `preset` / `mapping`, `create_missing`, `mirror` (explicit `[source, target]` pairs), `export` (`fbx`/`glb`), `strict` (missing visemes fail the chain instead of warning) |
| orient | `mode`: `direction-normal` (arrows along vertex normals) or `direction-attribute` (+ `attribute`), `basis`, `scale`. Check-only: it never modifies the artifact |
| stl | `export_scale` (1.0; use 1000 if your model is in meters and you want the STL read as mm) |

### Verdicts

Every stage lands as `pass`, `warn`, `fail`, `error`, or `skipped`.
Findings are not failures: viseme-doctor reporting 15 missing
visemes is a `warn` (your avatar has a problem; the tool did its
job) unless you set `strict`. A stage's own hard verdict —
ripdoctor's FAIL, stl-doctor's unhealthy, a cleanup output that
isn't watertight, a tool that errors or produces nothing — is a
`fail`/`error`, and **the chain stops there**. Later stages are
recorded as skipped, never silently dropped.

The combined report is `chain-report.json` + `chain-report.md` in
the output dir, one folder per stage (`01-ripdoctor/`, …) holding
that stage's own report files and logs.

## Usage

```bash
python3 doctor_chain.py examples/avatar-chain.json
python3 doctor_chain.py my-chain.yaml --out build/preflight
python3 doctor_chain.py my-chain.json --skip cleanup,stl
```

Exit codes: `0` chain passed (warnings allowed), `1` a stage
failed/errored, `2` manifest/usage error.

## What it was tested on

- **Driver logic**: 14 unit tests with fake doctors and a fake
  Blender (`tests/run_tests.py`, no Blender needed) — stage order,
  skipping, verdict mapping, stop-on-failure with evidence, YAML
  loading, tool resolution.
- **End to end** (`tests/test_e2e.py`, Blender 4.2.3): two real
  chains over real assets, with every stage cross-referenced against
  the same tool run standalone on the same input:
  - a game-rip avatar FBX (15,197 verts, 44 shape keys, **zero**
    standard visemes): ripdoctor probe → viseme remediation (57
    keys after, blink satisfied, 0 missing) → orient check (an arrow
    on all 15,197 vertices) → STL health check, which **fails** —
    a skinned avatar is not a watertight solid, and the chain says
    so with stl-doctor's evidence attached instead of papering
    over it;
  - a raw AI-generator GLB (1,497,650 tris of soup): cleanup to a
    solid whose before/after audit is byte-identical to
    ai-mesh-cleanup run standalone, viseme analyze (warn: no shape
    keys — correct for a cleaned generator mesh), orient check —
    and an STL **fail** that is the chain's best trick: cleanup's
    in-scene audit reports a watertight 25,000 triangles, but the
    GLB it exports silently drops 9 slivers that go degenerate in
    float32, and stl-doctor on the shipped file finds 24,991
    triangles and 27 boundary edges. Neither tool alone surfaces
    that gap. The chain did, with both reports attached.

## Known limits (v1, stated on purpose)

- viseme-doctor and orient-doctor are Blender-internal by design;
  the chain routes through Blender (import → doctor → export)
  rather than pretending they are file tools.
- The orient stage verifies that orientation data *flows and
  draws* on your model (arrow instances on every vertex, realized
  and counted). The numeric precision of the arrows themselves is
  orient-doctor's own test suite's job (39/39 there), not re-proven
  per chain run.
- Cleanup destroys UVs and materials (voxel remeshing does); a
  texture re-bake stage is future work, not a hidden feature.
- One artifact flows through the chain; multi-model kits are
  one manifest per model, for now.

## License

MIT — Copyright (c) 2026 1915rising. The tools it orchestrates keep
their own licenses (orient-doctor is GPL-3.0-or-later; see above).
