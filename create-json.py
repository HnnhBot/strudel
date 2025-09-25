#!/usr/bin/env python3
import argparse, json, re
import unicodedata as ud
from pathlib import Path

AUDIO_EXTS = {".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg"}

# flats everywhere; keep f#
SHARP2FLAT = {"c#": "db", "d#": "eb", "f#": "f#", "g#": "ab", "a#": "bb"}

GENERIC_FOLDERS = {
    "loops","loop","one shots","oneshots","one_shots","samples","audio","stems",
    "custom","customs","sounds","sound","fx","sfx","effects","drums"
}

DRUM_LOOP_GROUPS = {
    "breaks": "breaks",
    "claps": "claps",
    "shakers": "shakers",
    "woodblock": "woodblock",
    "woodblck": "woodblock",
}

def lc(s:str)->str: return s.lower()
def slug(s:str)->str: return re.sub(r"[^a-z0-9]+","_",lc(s)).strip("_")
def to_posix(p:Path)->str: return p.as_posix()

def normalize_note(note:str)->str:
    n = lc(note)
    if n.endswith("#"): return SHARP2FLAT.get(n, n)  # convert sharps except f#
    if n.endswith("b"): return n
    return n

# ---------- detectors ----------
BPM_TOKEN = re.compile(r"(?:^|[ _\-])(\d{2,3})(?=$|[ _\-])")
KEY_WITH_OCT = re.compile(r"(?:^|[^A-Za-z])([A-Ga-g])([#b]?)(\d)(?=[^A-Za-z0-9]|$)")
KEY_TOKEN   = re.compile(r"(?:^|[ _\-])([A-Ga-g])([#b]?)(maj|min|m)?(?=$|[ _\-\d])")

def get_bpm(stem:str) -> int|None:
    s = lc(stem)
    cands = [int(m.group(1)) for m in BPM_TOKEN.finditer(s)]
    cands = [b for b in cands if 40 <= b < 200]   # ignore 313 etc.
    return cands[-1] if cands else None

def get_key(stem:str):
    s = lc(stem)
    m = KEY_WITH_OCT.search(s)
    if m:
        root = normalize_note(m.group(1) + (m.group(2) or ""))
        return {"root": root, "minor": False, "oct": int(m.group(3))}
    m = KEY_TOKEN.search(s)
    if m:
        root = normalize_note(m.group(1) + (m.group(2) or ""))
        qual = (m.group(3) or "").lower()
        return {"root": root, "minor": (qual in ("m","min")), "oct": None}
    return None

# ---------- path helpers ----------
def has_seg(rel:str, name:str) -> bool:
    return any(slug(seg) == name for seg in rel.split("/"))

def is_loops_folder(rel:str) -> bool:
    return has_seg(rel, "loop") or has_seg(rel, "loops")

def is_drums_loops(rel:str) -> bool:
    return has_seg(rel, "drums") and is_loops_folder(rel)

def detect_drums_group(rel:str) -> str|None:
    for seg in rel.split("/"):
        s = slug(seg)
        if s in DRUM_LOOP_GROUPS:
            return DRUM_LOOP_GROUPS[s]
    return None

def guess_instrument(rel:str) -> str:
    segs = [slug(s) for s in rel.split("/")]
    for seg in reversed(segs[:-1]):
        if seg.replace("_"," ") not in GENERIC_FOLDERS:
            if seg.endswith("s") and not seg.endswith("ss"): seg = seg[:-1]
            return seg or "inst"
    return slug(Path(rel).stem).split("_")[0] or "inst"

def walk_audio(root:Path):
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
            yield p

def ensure_map(bucket):
    return bucket if isinstance(bucket, dict) else {"unpitched": list(bucket) if bucket else []}

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(description="Strudel manifest builder (BPM+Key loops; keymapped one-shots).")
    ap.add_argument("--root", required=True, help="Folder to scan")
    ap.add_argument("--base", required=True, help="CDN base URL for _base")
    ap.add_argument("--prefix", default=None, help="Key prefix (default: slug of root folder)")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    prefix = args.prefix or slug(root.name)

    manifest = {"_base": args.base}
    buckets: dict[str, list|dict] = {}

    for p in walk_audio(root):
        rel = to_posix(p.relative_to(root))
        rel = ud.normalize("NFC", rel)
        stem = p.stem

        bpm  = get_bpm(stem)
        keyi = get_key(stem)

        # ---- LOOPS ----
        if is_loops_folder(rel) or (bpm is not None and keyi is not None):
            if is_drums_loops(rel):
                grp = detect_drums_group(rel)
                if grp:
                    key = f"{prefix}_{grp}" + (f"_{bpm}" if bpm is not None else "")
                    buckets.setdefault(key, [])
                    if rel not in buckets[key]: buckets[key].append(rel)
                    continue
            # generic loops: prefix_BPM_key (key includes 'm' if minor), or prefix_BPM if no key
            if bpm is not None and keyi is not None:
                loop_key = keyi["root"] + ("m" if keyi["minor"] else "")
                key = f"{prefix}_{bpm}_{loop_key}"
            elif bpm is not None:
                key = f"{prefix}_{bpm}"
            else:
                key = f"{prefix}_loops"
            buckets.setdefault(key, [])
            if rel not in buckets[key]: buckets[key].append(rel)
            continue

        # ---- ONE-SHOTS / NON-LOOPS ----
        instrument = guess_instrument(rel)
        keyname = f"{prefix}_{instrument}"
        # keymap when we can resolve a pitch; octave from filename if present, else default 4
        if keyi is not None:
            pitch = f"{keyi['root']}{keyi['oct'] if keyi['oct'] is not None else 4}"
            buckets[keyname] = ensure_map(buckets.get(keyname, {}))
            buckets[keyname].setdefault(pitch, [])
            if rel not in buckets[keyname][pitch]: buckets[keyname][pitch].append(rel)
        else:
            if isinstance(buckets.get(keyname, []), dict):
                buckets[keyname].setdefault("unpitched", [])
                if rel not in buckets[keyname]["unpitched"]: buckets[keyname]["unpitched"].append(rel)
            else:
                buckets.setdefault(keyname, [])
                if rel not in buckets[keyname]: buckets[keyname].append(rel)

    # stable sort
    def sort_map(d): return {k: sorted(v) for k, v in sorted(d.items())}
    for k, v in list(buckets.items()):
        manifest[k] = sort_map(v) if isinstance(v, dict) else sorted(v)

    print(json.dumps(dict(sorted(manifest.items())), indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
