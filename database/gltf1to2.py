"""Convert glTF 1.0 binaries (.glb) to glTF 2.0 — so old model sets "just work".

Flightradar24's published aircraft models (github.com/Flightradar24/fr24-3d-models, GPLv2)
are COLLADA2GLTF-era glTF *1.0* binaries, which three.js (and every modern loader) refuses.
The 1.0 -> 2.0 conversion is mostly mechanical for that generator's output and this module
does exactly that, in stdlib Python, targeting what those files actually contain:

  * name-keyed dicts (accessors/bufferViews/materials/meshes/nodes/...) -> arrays + indices
  * one "binary_glTF" body buffer, carried over byte-for-byte
  * per-accessor bufferViews in the output (sidesteps every v2 byteStride sharing rule)
  * KHR_binary_glTF embedded images -> v2 bufferView images
  * technique/values materials -> PBR metallic-roughness (diffuse texture/colour carried
    over; a uniform painted-metal roughness looks right for airliner liveries)
  * nodes: children/meshes by name -> indices, matrices kept (they encode the Z-up->Y-up
    axis conversion COLLADA2GLTF baked into the root)

Shaders/programs/techniques/animations/skins/cameras are dropped. convert() raises
ValueError with a human-readable reason when the file isn't something it understands.
"""

import json
import struct

_COMP_SIZE = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
_TYPE_N = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}


def is_glb1(data):
    return len(data) >= 12 and data[:4] == b"glTF" and struct.unpack("<I", data[4:8])[0] == 1


def _parse_glb1(data):
    if len(data) < 20:
        raise ValueError("truncated .glb")
    magic, version, total = struct.unpack("<4sII", data[:12])
    if magic != b"glTF" or version != 1:
        raise ValueError("not a glTF 1.0 binary")
    clen, cfmt = struct.unpack("<II", data[12:20])
    if cfmt != 0:
        raise ValueError("unsupported glTF 1.0 content format %d" % cfmt)
    if 20 + clen > len(data):
        raise ValueError("corrupt .glb (content runs past end of file)")
    gltf = json.loads(data[20:20 + clen].decode("utf-8"))
    body = data[20 + clen:]
    return gltf, body


def convert(data):
    """glTF 1.0 .glb bytes -> glTF 2.0 .glb bytes (raises ValueError if not convertible)."""
    g1, body = _parse_glb1(data)

    views1 = g1.get("bufferViews") or {}
    acc1 = g1.get("accessors") or {}
    out = {"asset": {"version": "2.0", "generator": "tar1090 gltf1to2"},
           "buffers": [{"byteLength": len(body)}],
           "bufferViews": [], "accessors": []}

    # every v1 accessor gets its own v2 bufferView with an absolute offset into the body —
    # no byteStride sharing rules to trip over, identical bytes on the GPU
    acc_idx = {}
    for name, a in acc1.items():
        v1v = views1.get(a.get("bufferView"))
        if not v1v:
            continue
        comp = a["componentType"]; n = _TYPE_N[a["type"]]
        elem = _COMP_SIZE[comp] * n
        stride = int(a.get("byteStride") or 0)
        count = int(a["count"])
        off = int(v1v.get("byteOffset") or 0) + int(a.get("byteOffset") or 0)
        span = (stride if stride > elem else elem) * (count - 1) + elem if count else 0
        view = {"buffer": 0, "byteOffset": off, "byteLength": span}
        tgt = v1v.get("target")
        if tgt in (34962, 34963):
            view["target"] = tgt
        if stride > elem and tgt != 34963:
            view["byteStride"] = stride
        acc2 = {"bufferView": len(out["bufferViews"]), "byteOffset": 0,
                "componentType": comp, "count": count, "type": a["type"]}
        for k in ("min", "max", "normalized"):
            if k in a:
                acc2[k] = a[k]
        out["bufferViews"].append(view)
        acc_idx[name] = len(out["accessors"])
        out["accessors"].append(acc2)

    # POSITION accessors must carry min/max in v2 — compute from the body if v1 lacked them
    def _fix_minmax(ai):
        a = out["accessors"][ai]
        if "min" in a and "max" in a:
            return
        v = out["bufferViews"][a["bufferView"]]
        n = _TYPE_N[a["type"]]; comp = a["componentType"]
        elem = _COMP_SIZE[comp] * n
        stride = v.get("byteStride") or elem
        fmt = {5126: "f", 5123: "H", 5125: "I", 5122: "h", 5121: "B", 5120: "b"}[comp]
        mins = [float("inf")] * n; maxs = [float("-inf")] * n
        for i in range(a["count"]):
            base = v["byteOffset"] + i * stride
            vals = struct.unpack_from("<" + fmt * n, body, base)
            for k in range(n):
                mins[k] = min(mins[k], vals[k]); maxs[k] = max(maxs[k], vals[k])
        a["min"] = mins; a["max"] = maxs

    # images (KHR_binary_glTF) / samplers / textures
    img_idx, out_images = {}, []
    for name, im in (g1.get("images") or {}).items():
        ext = (im.get("extensions") or {}).get("KHR_binary_glTF")
        if not ext:
            continue                                        # external-URI images: not in FR24 files
        v1v = views1.get(ext.get("bufferView"))
        if not v1v:
            continue
        out["bufferViews"].append({"buffer": 0, "byteOffset": int(v1v.get("byteOffset") or 0),
                                   "byteLength": int(v1v.get("byteLength") or 0)})
        img_idx[name] = len(out_images)
        out_images.append({"bufferView": len(out["bufferViews"]) - 1,
                           "mimeType": ext.get("mimeType") or "image/png"})
    if out_images:
        out["images"] = out_images
    smp_idx, out_samplers = {}, []
    for name, s in (g1.get("samplers") or {}).items():
        smp_idx[name] = len(out_samplers)
        out_samplers.append({k: s[k] for k in ("magFilter", "minFilter", "wrapS", "wrapT") if k in s})
    if out_samplers:
        out["samplers"] = out_samplers
    tex_idx, out_textures = {}, []
    for name, t in (g1.get("textures") or {}).items():
        if t.get("source") not in img_idx:
            continue
        tx = {"source": img_idx[t["source"]]}
        if t.get("sampler") in smp_idx:
            tx["sampler"] = smp_idx[t["sampler"]]
        tex_idx[name] = len(out_textures)
        out_textures.append(tx)
    if out_textures:
        out["textures"] = out_textures

    # materials: technique 'values' -> PBR metallic-roughness
    mat_idx, out_mats = {}, []
    for name, m in (g1.get("materials") or {}).items():
        vals = m.get("values") or {}
        diff = vals.get("diffuse")
        pbr = {"metallicFactor": 0.1, "roughnessFactor": 0.55}
        mat = {"name": m.get("name") or name, "doubleSided": True,
               "pbrMetallicRoughness": pbr}
        if isinstance(diff, str) and diff in tex_idx:
            pbr["baseColorTexture"] = {"index": tex_idx[diff]}
        elif isinstance(diff, (list, tuple)) and len(diff) >= 3:
            col = [float(diff[0]), float(diff[1]), float(diff[2]),
                   float(diff[3]) if len(diff) > 3 else 1.0]
            pbr["baseColorFactor"] = col
            if col[3] < 0.999:
                mat["alphaMode"] = "BLEND"
        mat_idx[name] = len(out_mats)
        out_mats.append(mat)
    if out_mats:
        out["materials"] = out_mats

    # meshes
    mesh_idx, out_meshes = {}, []
    for name, me in (g1.get("meshes") or {}).items():
        prims = []
        for p in me.get("primitives") or []:
            attrs = {k: acc_idx[v] for k, v in (p.get("attributes") or {}).items() if v in acc_idx}
            if "POSITION" not in attrs:
                continue
            _fix_minmax(attrs["POSITION"])
            prim = {"attributes": attrs}
            if p.get("indices") in acc_idx:
                prim["indices"] = acc_idx[p["indices"]]
            if p.get("material") in mat_idx:
                prim["material"] = mat_idx[p["material"]]
            if p.get("mode") is not None and p["mode"] != 4:
                prim["mode"] = p["mode"]
            prims.append(prim)
        if not prims:
            continue
        mesh_idx[name] = len(out_meshes)
        out_meshes.append({"name": name, "primitives": prims})
    if not out_meshes:
        raise ValueError("no triangle meshes found in this glTF 1.0 file")
    out["meshes"] = out_meshes

    # nodes: children/meshes by name -> indices; a v1 node with several meshes becomes a
    # parent with one child per mesh (v2 allows a single mesh per node)
    nodes1 = g1.get("nodes") or {}
    node_idx, out_nodes = {}, []
    for name in nodes1:                                     # first pass: allocate slots
        node_idx[name] = len(out_nodes)
        out_nodes.append({"name": name})
    for name, n1 in nodes1.items():
        n2 = out_nodes[node_idx[name]]
        if isinstance(n1.get("matrix"), list) and len(n1["matrix"]) == 16:
            ident = [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]
            if n1["matrix"] != ident:
                n2["matrix"] = n1["matrix"]
        kids = [node_idx[c] for c in (n1.get("children") or []) if c in node_idx]
        meshes = [mesh_idx[m] for m in (n1.get("meshes") or []) if m in mesh_idx]
        if len(meshes) == 1:
            n2["mesh"] = meshes[0]
        elif meshes:
            for mi in meshes:
                kids.append(len(out_nodes))
                out_nodes.append({"mesh": mi})
        if kids:
            n2["children"] = kids
    out["nodes"] = out_nodes

    scenes1 = g1.get("scenes") or {}
    scene_name = g1.get("scene")
    roots = (scenes1.get(scene_name) or {}).get("nodes") if scene_name else None
    if not roots:                                           # fall back: every un-parented node
        child_set = set()
        for n1 in nodes1.values():
            child_set.update(n1.get("children") or [])
        roots = [n for n in nodes1 if n not in child_set]
    out["scenes"] = [{"nodes": [node_idx[r] for r in roots if r in node_idx]}]
    out["scene"] = 0

    # assemble GLB v2: 12-byte header + JSON chunk (space-padded) + BIN chunk (zero-padded)
    js = json.dumps(out, separators=(",", ":")).encode("utf-8")
    js += b" " * ((4 - len(js) % 4) % 4)
    bin_ = body + b"\0" * ((4 - len(body) % 4) % 4)
    total = 12 + 8 + len(js) + 8 + len(bin_)
    return b"".join([struct.pack("<4sII", b"glTF", 2, total),
                     struct.pack("<II", len(js), 0x4E4F534A), js,
                     struct.pack("<II", len(bin_), 0x004E4942), bin_])
