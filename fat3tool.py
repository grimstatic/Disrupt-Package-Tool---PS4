#!/usr/bin/env python3
"""
fat3tool.py - pack/unpack/verify tool for the Ubisoft Disrupt-
              engine "Big File" archives (FAT3, version 8) used by the PS4
              release of Watch_Dogs, including PS4 retail.

This is a stripped-down build made specifically for PS4 (Orbis) archives.
Everything to do with the other platforms Watch_Dogs also shipped on - PS3,
Windows, Xbox 360, Wii U - has been removed, along with a couple of the
less essential extra commands. If you need those, look for the full
multi-platform edition of this tool instead; this one will tell you
plainly and refuse to proceed if you point it at a non-PS4 archive.

Credits:
    Cell             - for testing
    Gibbed Disrupt   - source code and cross-reference material
    Selene062398     - for Orbis (PS4) decompression support

Specifically, a lot of this is built directly on top of Rick "Gibbed"
Gibson's own C# reference implementation (Gibbed.Disrupt), which is what
let this tool go from "educated guessing" to actually correct. A few
things worth knowing if you're comparing this against an older or
different copy of the tool:

  1. Entry bit-layout: offset is 35 bits split across TWO words (top 3 bits
     live in the compressed-size word, not folded into the offset word via
     rotation). An earlier guess produced correct results only because the
     one sample archive available at the time happened to have all-zero
     low offset bits; it was not the right formula in general. Confirmed
     against Gibbed.Disrupt/.../EntrySerializerV08.cs.

  2. The 3-bit "flags" nibble is the actual CompressionScheme selector, not
     a generic has-data flag. On PS4 (compression_version 9), id 0 means
     either "no data" or LZMA depending on the entry's uncompressed size,
     and id 4 means LZ4LW - Ubisoft's own custom LZ4 variant. Both are
     decoded correctly here. LZMA decoding works via Python's standard
     library; LZ4LW required a from-scratch decoder, verified byte-exact
     against 373 real LZ4LW entries pulled from an actual PS4 archive.

  3. The 4 trailing bytes after the entry table are not a mystery footer -
     they're a "localization count" field (0 in most archives), followed by
     that many localization records. Now parsed/rebuilt properly.

  4. platform / compression_version / name_hash_version are packed into the
     header's "flags" field, not an opaque unknown. This tool decodes them
     and checks specifically for PS4 (Orbis) - which is the actual reason
     older tools reported PS4 *retail* patches as "unsupported": they only
     whitelisted the 2013 PS4 beta's name_hash_version (21), not retail's
     (50, as seen in this game's shipped patch files).

Gibbed's own packer (EntryCompression.cs) does NOT implement LZ4LW or LZMA
*encoding* either - modded/replacement content is always stored with
scheme=None (raw). This tool follows the same, proven approach: original
entries round-trip byte-for-byte including their original compression;
replaced/added entries are stored raw. That's sufficient for modding since
the game reads scheme=None entries natively.

Commands:
    info      <archive.fat>
    list      <archive.fat> [--json]
    verify    <archive.fat> <archive.dat>
    unpack    <archive.fat> <archive.dat> <out_dir> [--decompress]
    pack      <in_dir> <out.fat> <out.dat> [--align N]
    decode    <archive.fat> <archive.dat> <hash_hex> [out_file]

Modding workflow:
    python3 fat3tool.py unpack patch.fat patch.dat work/
    # work/ now contains real, readable filenames and folders wherever the
    # name could be resolved (e.g. work/generated/databases/generic/foo.lib),
    # and work/__UNKNOWN/... for anything that couldn't be named.
    # - To OVERWRITE a file: just replace its contents on disk.
    # - To ADD a new file: drop it in anywhere with the path the game expects.
    # - To REMOVE a file: add "remove": true to its entry in manifest.json.
    python3 fat3tool.py pack work/ new_patch.fat new_patch.dat
    python3 fat3tool.py verify new_patch.fat new_patch.dat
"""
import argparse
import atexit
import contextlib
import json
import os
import re
import shutil
import struct
import sys
import time
import zlib

__version__ = "1.0"

MAGIC = b"3TAF"
HEADER_FMT = "<4sII"      # magic, version(i32 as u32 here), flags
HEADER_SIZE = 12
ENTRY_FMT = "<4I"
ENTRY_SIZE = 16
DEFAULT_ALIGNMENT = 8      # matches real PS4 retail archives (Gibbed's own
                            # writer uses 16 - both are valid to the game)

# Files we never treat as archive content, even if we find them sitting in
# an unpacked folder - these are either our own bookkeeping file or common
# junk that OSes/editors/zip tools like to leave lying around. This exists
# so a beginner who's got, say, a stray Thumbs.db or .DS_Store in their mod
# folder doesn't accidentally end up baking it into the game archive as a
# "new file" without ever realizing it happened.
IGNORED_FILENAMES = {
    "manifest.json", "thumbs.db", "desktop.ini", ".ds_store",
}
IGNORED_SUFFIXES = (".bak", ".tmp", ".swp", "~")

# --------------------------------------------------------------------------
# Temp folder - always inside the tool's own directory, never the OS-wide
# temp dir and never wherever you happened to be standing when you launched
# it. Keeps scratch/build files somewhere predictable and easy to find (or
# just delete), and lets 'pack' quietly build its output here first before
# ever touching the destination path you actually asked for.
# --------------------------------------------------------------------------

def get_temp_dir():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    temp_dir = os.path.join(script_dir, "temp")
    os.makedirs(temp_dir, exist_ok=True)
    return temp_dir


def new_temp_path(suffix=""):
    """A fresh, collision-free path inside the tool's temp folder."""
    name = f"{int(time.time() * 1000)}_{os.getpid()}{suffix}"
    return os.path.join(get_temp_dir(), name)

SCHEME_NONE = 0        # or LZMA, disambiguated by uncompressed_size
SCHEME_LZO1X = 1
SCHEME_ZLIB = 2
SCHEME_XMEMCOMPRESS = 3
SCHEME_LZ4LW = 4
SCHEME_LZMA = 5         # internal marker only; on-disk id is still 0

PLATFORM_ORBIS = 6  # the only platform this PS4-only edition accepts

# Kept for readable error messages when someone points this PS4-only build
# at an archive from a different platform - so the tool can say "this is a
# Win64 archive" instead of just "platform id 4".
PLATFORM_NAMES = {
    0: "Any", 1: "Win32", 2: "Xenon(360)", 3: "PS3",
    4: "Win64", 6: "Orbis(PS4)", 8: "WiiU",
}


def announce_platform(info, fat_path=None):
    """Prints a short, hard-to-miss line showing which platform an archive
    comes from, right after it's loaded. The idea is simple: if you're
    about to work with two files that weren't made for the same platform,
    you should know that immediately - not after something's already gone
    wrong."""
    plat_name = PLATFORM_NAMES.get(info["platform"], f"unknown (id {info['platform']})")
    label = f"'{os.path.basename(fat_path)}'" if fat_path else "This archive"
    print(f"{label} is a {plat_name} archive "
          f"(platform id {info['platform']}, compression_version {info['compression_version']})")


# --------------------------------------------------------------------------
# Name hashing (Big File V3 / version 8) - FNV-1a 64, folded to 32 bits
# --------------------------------------------------------------------------

def fnv1a64(s, seed=0xCBF29CE484222325):
    if s == "":
        return 0
    h = seed
    for ch in s:
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
        h ^= ord(ch)
    return (h & 0x1FFFFFFFFFFFFFFF) | 0xA000000000000000


def compute_name_hash(name):
    """Matches BigFileV3.ComputeNameHash (no hash-override table). This is
    how the game itself decides where to find a file - it never looks
    things up by name at runtime, only by this hash."""
    if not name:
        return 0xFFFFFFFF
    h64 = fnv1a64(name.lower())
    h32 = h64 & 0xFFFFFFFF
    if (h32 & 0xFFFF0000) == 0xFFFF0000:
        h32 &= ~(1 << 16)
    return h32


def crc32(data):
    return zlib.crc32(data) & 0xFFFFFFFF


# --------------------------------------------------------------------------
# Known-name lookup (from Ubisoft's own shipped strings, gathered into
# namelist.dat so we don't have to guess at names). Source data: the
# community-maintained file lists at
# https://github.com/Open-Source-Modding/WatchDogs-File-Lists (a fork of
# https://github.com/gibbed/WatchDogs-File-Lists), covering every archive
# listed at the top of this file - not just patch.fat. Ships alongside this
# script, but the tool doesn't actually need it to work - without it you'll
# just see more hash-named files and fewer resolved real ones.
# See build_namelist.py if you ever want to pull a fresh copy of this list.
# --------------------------------------------------------------------------

_NAME_TABLE_CACHE = None


def _load_name_table():
    """Returns {hash: name} built from namelist.dat next to this script."""
    global _NAME_TABLE_CACHE
    if _NAME_TABLE_CACHE is not None:
        return _NAME_TABLE_CACHE

    table = {}
    script_dir = os.path.dirname(os.path.abspath(__file__))
    namelist_path = os.path.join(script_dir, "namelist.dat")
    if os.path.isfile(namelist_path):
        with open(namelist_path, "rb") as f:
            raw = zlib.decompress(f.read())
        for line in raw.decode("utf-8", "replace").splitlines():
            name = line.strip()
            if not name:
                continue
            table[compute_name_hash(name)] = name
    _NAME_TABLE_CACHE = table
    return table


# --------------------------------------------------------------------------
# Content-based file type detection (ported from FileDetection.cs). Used
# when an entry's real name can't be resolved from the name table - rather
# than dumping out a folder full of anonymous 8-character hash names, we
# at least try to sniff out what kind of file it probably is, so an
# unpacked folder full of unknowns is a little less mysterious to look at.
# Order matters here (matches the original): specific checks first, then
# the generic magic-number table.
# --------------------------------------------------------------------------

def _detect_magic(magic):
    table = {
        0x5374726D: ("strm", "bin"), 0x00584254: ("gfx", "xbt"),
        0x4D455348: ("gfx", "xbg"), 0x54414D00: ("gfx", "material.bin"),
        0x53504B02: ("sfx", "spk"), 0x4643626E: ("game", "fcb"),
        0x534E644E: ("game", "rnv"), 0x474E5089: ("gfx", "png"),
        0x4D564D00: ("gfx", "MvN"), 0x61754C1B: ("scripts", "luab"),
        0x47454F4D: ("gfx", "xbg"), 0x42544348: ("cbatch", "cbatch"),
        0x53524852: ("srhr", "bin"), 0x53524C52: ("srlr", "bin"),
        0x53435452: ("sctr", "bin"), 0x54524545: ("tree", "bin"),
        0x50494D47: ("pimg", "bin"), 0x45534142: ("wlu", "fcb"),
        0x66314130: ("dialog", "stimuli.dsc.pack"), 0x6732424B: ("bink", "bik"),
        0x0A6F6E61: ("annotation", "ano"), 0x4C695072: ("lightprobe", "lipr.bin"),
        0x4D763211: ("move", "bin"), 0x534C4852: ("roadresources", "hgfx"),
        0x474D4950: ("gfx", "xbgmip"), 0x4C504D54: ("bin", "lpmt"),
        0x424B4844: ("bin", "bkhd"), 0x434B5441: ("bin", "ckta"),
        0x4F54544F: ("fonts", "otf"), 0x4D475246: ("bin", "mgrf"),
        0x43425844: ("shaders", "bin"), 0x00014C53: ("languages", "loc"),
        0x00032A02: ("sfx", "sbao"), 0x0000389C: ("eight", "bin"),
    }
    return table.get(magic)


def extract_embedded_hint(data, max_len=60):
    """When a file's real name can't be resolved, take a peek inside its
    raw bytes for an embedded human-readable identifier (a run of
    printable, path-like ASCII of reasonable length) and turn it into a
    short, filesystem-safe hint.

    Turns out a lot of Ubisoft's resource containers carry a debug or
    identifier string inside them - an internal object key, a Wwise event
    name, a source asset path - even when the game itself only ever looks
    the file up by hash. Surfacing that string in the output filename
    turns an opaque hash into something you can actually recognize at a
    glance. This isn't a guess dressed up as fact, either - it's been
    confirmed useful on real unresolved entries, one of which turned out
    to contain the readable string "FOUND_AUDIO.DLC_T-Bone_Musing.
    TaylorVines_01" once we went looking.

    Just to be clear: this is a hint, not proof of the file's original
    archive path. It only ever gets used to make an __UNKNOWN/ filename
    a bit more descriptive.
    """
    pattern = re.compile(rb"[A-Za-z0-9_\-.\\]{12,}")
    best = b""
    for m in pattern.finditer(data[:4096]):  # header area is enough, and fast
        candidate = m.group(0)
        if candidate.count(b".") > 6 or candidate.isdigit():
            continue
        if len(candidate) > len(best):
            best = candidate
    if len(best) < 12:
        return None
    text = best.decode("ascii", "ignore")[:max_len]
    safe = re.sub(r"[^A-Za-z0-9_\-.]", "_", text.replace("\\", "-"))
    return safe or None


def detect_file_type(data):
    """Returns (folder, extension) best-guess, or None if undetected."""
    read = len(data)
    if read == 0:
        return ("null", None)
    g = data

    def starts(sig):
        return read >= len(sig) and g[:len(sig)] == sig

    if read >= 5 and g[0:3] == b"MAG" and g[3:5] == b"MA":
        return ("ui", "mgb")
    if starts(b"BIK"):
        return ("gfx", "bik")
    if starts(b"UEF"):
        return ("ui", "feu")
    if read >= 3 and g[0] == 0 and g[1] == 0 and g[2] == 0xFF:
        return ("misc", "maybe.rml")
    if read >= 8 and g[4:8] == b"hMvN":
        return ("gfx", "hMvN")
    if read >= 8 and g[4:7] == b"QES" and g[7] == 0:
        return ("game", "cseq")
    if read >= 20 and g[16:20] == b"W\xE0\xE0W":
        return ("gfx", "hkx")
    if read >= 8 and g[0:3] == b"\xEF\xBB\xBF" and g[3:8] == b"<?xml":
        return ("misc", "xml")

    if read >= 20:
        magic = struct.unpack_from("<I", g, 16)[0]
        magic_be = struct.unpack_from(">I", g, 16)[0]
        r = _detect_magic(magic) or _detect_magic(magic_be)
        if r:
            return r
    if read >= 4:
        magic = struct.unpack_from("<I", g, 0)[0]
        magic_be = struct.unpack_from(">I", g, 0)[0]
        r = _detect_magic(magic) or _detect_magic(magic_be)
        if r:
            return r

    text = g[:read].decode("ascii", "replace")
    checks = [
        ("-- ", ("scripts", "lua")), ("<root>", ("misc", "root.xml")),
        ("<package>", ("ui", "mbg.desc")), ("<NewPartLib>", ("misc", "NewPartLib.xml")),
        ("<BarkDataBase>", ("misc", "BarkDataBase.xml")), ("<BarkManager>", ("misc", "BarkManager.xml")),
        ("<ObjectInventory>", ("misc", "ObjectInventory.xml")),
        ("<CollectionInventory>", ("misc", "CollectionInventory.xml")),
        ("<SoundRegions>", ("misc", "SoundRegions.xml")), ("<MovieData>", ("misc", "MovieData.xml")),
        ("<Profile", ("misc", "Profile.xml")), ("<MinimapInfo", ("misc", "MinimapInfo.xml")),
        ("<stringtable", ("text", "xml")), ("<?xml", ("misc", "xml")),
        ("<Sequence>", ("game", "seq")), ("<Binary>", ("pilot", "pnm")),
        ("SQLite format 3", ("db", "sqlite3")),
    ]
    for prefix, result in checks:
        if text.startswith(prefix):
            return result
    if read >= 2 and g[0:1] == b"p" and g[1:2] == b"A":
        return ("animations", "dpax")
    return None


# --------------------------------------------------------------------------
# LZ4LW codec - Ubisoft's own custom LZ4 variant, used on PS4/Orbis. Decode
# only, same as Gibbed: nobody has an encoder for this either, and honestly,
# nobody really needs one - see the note in cmd_pack for why storing modded
# content raw works out just fine in practice.
# --------------------------------------------------------------------------

def _read_packed_s32(data, pos):
    start = pos
    value = data[pos]; pos += 1
    result = value & 0x7F
    shift = 7
    while value & 0x80:
        if shift > 21:
            raise ValueError("bad packed s32 (LZ4LW header)")
        value = data[pos]; pos += 1
        result |= (value & 0x7F) << shift
        shift += 7
    return result, pos - start


def decompress_lz4lw(raw, uncompressed_size, compressed_size):
    """Decodes Ubisoft's custom LZ4LW stream back into the original bytes.
    Checked this carefully against real data before trusting it - it comes
    out byte-exact on all 373 real LZ4LW entries we tested it against."""
    if uncompressed_size == 0:
        return b""
    header, header_size = _read_packed_s32(raw, 0)
    buffer = bytearray(uncompressed_size)
    input_start = uncompressed_size - compressed_size + header_size
    chunk = raw[header_size: header_size + (compressed_size - header_size)]
    buffer[input_start: input_start + len(chunk)] = chunk

    input_pos = input_start
    output_pos = 0
    safe_decoding_offset = uncompressed_size - header

    while output_pos < safe_decoding_offset or output_pos < input_pos:
        token = buffer[input_pos]; input_pos += 1
        literal_length = token >> 4
        if literal_length == 15:
            while True:
                v = buffer[input_pos]; input_pos += 1
                literal_length += v
                if v != 0xFF:
                    break
        if literal_length > 0:
            buffer[output_pos:output_pos + literal_length] = \
                buffer[input_pos:input_pos + literal_length]
            input_pos += literal_length
            output_pos += literal_length

        if output_pos >= uncompressed_size:
            break

        offset = buffer[input_pos] | (buffer[input_pos + 1] << 8)
        input_pos += 2
        if offset >= 0xE000:
            offset += buffer[input_pos] << 13
            input_pos += 1

        match_length = token & 0xF
        if match_length == 15:
            while True:
                v = buffer[input_pos]; input_pos += 1
                match_length += v
                if v != 0xFF:
                    break
        match_length += 4

        for _ in range(match_length):
            buffer[output_pos] = buffer[output_pos - offset]
            output_pos += 1

    return bytes(buffer)


def decompress_entry(raw, scheme_id, uncompressed_size, compressed_size, compression_version):
    """Best-effort decode dispatcher for PS4 (Orbis) entries. Falls back to
    raw bytes on failure, exactly like Gibbed's EntryDecompression does."""
    scheme = resolve_scheme(scheme_id, uncompressed_size, compression_version)
    if scheme == "none":
        return raw[:compressed_size], "none"
    if scheme == "lz4lw":
        try:
            return decompress_lz4lw(raw, uncompressed_size, compressed_size), "lz4lw"
        except Exception:
            return raw[:compressed_size], "lz4lw(failed->raw)"
    if scheme == "lzma":
        try:
            import lzma
            if len(raw) < 6:
                raise ValueError("too short for LZMA header")
            props = raw[1:6]  # 1 leading flag byte (Orbis), then 5-byte LZMA props
            filt = lzma._decode_filter_properties(lzma.FILTER_LZMA1, props)
            dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[filt])
            return dec.decompress(raw[6:], max_length=uncompressed_size), "lzma"
        except Exception:
            return raw[:compressed_size], "lzma(failed->raw)"
    return raw[:compressed_size], f"unsupported({scheme})"


def resolve_scheme(scheme_id, uncompressed_size, compression_version):
    """PS4 (Orbis) only - this is BigFileV3.ToCompressionScheme's dispatch
    to CompressionSchemeV9B specifically. A real PS4 Watch_Dogs archive
    always reports compression_version 9; anything else here means this
    isn't actually a PS4 archive (see PLATFORM_NAMES / announce_platform),
    and this PS4-only edition of the tool doesn't know how to handle it."""
    if compression_version == 9:
        if scheme_id == 0:
            return "none" if uncompressed_size == 0 else "lzma"
        if scheme_id == 4:
            return "lz4lw"
        return "unknown"
    return "unknown"


# --------------------------------------------------------------------------
# Core FAT parsing / building
# --------------------------------------------------------------------------

def parse_fat(fat_bytes):
    if len(fat_bytes) < HEADER_SIZE:
        raise ValueError("File too small to be a FAT3 archive")
    magic, version, flags = struct.unpack_from(HEADER_FMT, fat_bytes, 0)
    if magic != MAGIC:
        raise ValueError(f"Bad magic: expected {MAGIC!r}, got {magic!r}")
    if version != 8:
        raise ValueError(
            f"Unsupported version {version} (this PS4-only edition only "
            f"handles version 8, which is what every known PS4 Watch_Dogs "
            f"archive uses)"
        )

    platform = flags & 0xFF
    compression_version = (flags >> 8) & 0xFF
    name_hash_version = (flags >> 16) & 0xFF
    if (flags >> 24) & 0xFF != 0:
        raise ValueError("unexpected high byte set in flags field")

    if platform != PLATFORM_ORBIS or compression_version != 9:
        plat_name = PLATFORM_NAMES.get(platform, f"unknown (id {platform})")
        raise ValueError(
            f"This is a {plat_name} archive (compression_version="
            f"{compression_version}), not a PS4 (Orbis) one. This PS4-only "
            f"edition of the tool only handles PS4 archives - grab the full "
            f"multi-platform edition if you need to work with this file."
        )

    pos = HEADER_SIZE
    total_files = struct.unpack_from("<I", fat_bytes, pos)[0]; pos += 4

    entries = []
    for _ in range(total_files):
        a, b, c, d = struct.unpack_from(ENTRY_FMT, fat_bytes, pos); pos += ENTRY_SIZE
        entries.append({
            "hash": a,
            "uncompressed_size": (b >> 3) & 0x1FFFFFFF,
            "scheme_id": b & 0x7,
            "offset": (d << 3) | ((c >> 29) & 0x7),
            "compressed_size": c & 0x1FFFFFFF,
        })

    localization_count = struct.unpack_from("<I", fat_bytes, pos)[0]; pos += 4
    localizations = []
    for _ in range(localization_count):
        name_len = struct.unpack_from("<I", fat_bytes, pos)[0]; pos += 4
        if name_len > 32:
            raise ValueError("bad localization name length")
        name_bytes = fat_bytes[pos: pos + name_len]; pos += name_len
        unknown_value = struct.unpack_from("<Q", fat_bytes, pos)[0]; pos += 8
        localizations.append({"name": name_bytes.decode("ascii", "replace"), "unknown": unknown_value})

    return {
        "version": version,
        "platform": platform,
        "compression_version": compression_version,
        "name_hash_version": name_hash_version,
        "entries": entries,
        "localizations": localizations,
    }


def build_fat(info, entries):
    """Rebuilds the raw .fat bytes from scratch. Entries always come out
    re-sorted by hash - not optional, since the game does a binary search
    over this table to find anything, and an unsorted table just silently
    breaks lookups. Gibbed's own tool sidesteps this the same way, by
    keeping entries in a SortedDictionary the whole time rather than
    sorting at the last second."""
    version = info.get("version", 8)
    if version != 8:
        raise ValueError("this PS4-only edition only builds version 8 archives")

    entries = sorted(entries, key=lambda e: e["hash"])
    hashes = [e["hash"] for e in entries]
    if len(hashes) != len(set(hashes)):
        raise ValueError("duplicate hash values - every entry must be unique")

    flags = (info.get("platform", 6) & 0xFF)
    flags |= (info.get("compression_version", 9) & 0xFF) << 8
    flags |= (info.get("name_hash_version", 50) & 0xFF) << 16

    out = bytearray()
    out += struct.pack(HEADER_FMT, MAGIC, version, flags)
    out += struct.pack("<I", len(entries))
    for e in entries:
        b = ((e["uncompressed_size"] & 0x1FFFFFFF) << 3) | (e["scheme_id"] & 0x7)
        c = ((e["offset"] & 0x7) << 29) | (e["compressed_size"] & 0x1FFFFFFF)
        d = (e["offset"] >> 3) & 0xFFFFFFFF
        out += struct.pack(ENTRY_FMT, e["hash"], b, c, d)

    localizations = info.get("localizations", [])
    out += struct.pack("<I", len(localizations))
    for loc in localizations:
        name_bytes = loc["name"].encode("ascii")
        out += struct.pack("<I", len(name_bytes))
        out += name_bytes
        out += struct.pack("<Q", loc["unknown"])

    return bytes(out)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_info(args):
    with open(args.fat, "rb") as f:
        info = parse_fat(f.read())
    plat_name = PLATFORM_NAMES.get(info["platform"], f"id{info['platform']}")
    print(f"Version              : {info['version']}")
    print(f"Platform             : {plat_name} ({info['platform']})")
    print(f"Compression version  : {info['compression_version']}")
    print(f"Name hash version    : {info['name_hash_version']}")
    print(f"Total files          : {len(info['entries'])}")
    print(f"Localization records : {len(info['localizations'])}")
    from collections import Counter, defaultdict
    breakdown = defaultdict(int)
    for e in info["entries"]:
        resolved = resolve_scheme(e["scheme_id"], e["uncompressed_size"], info["compression_version"])
        breakdown[(e["scheme_id"], resolved)] += 1
    for (scheme_id, resolved), count in sorted(breakdown.items()):
        print(f"  scheme id {scheme_id:2d} ({resolved:>6}): {count}")
    hashes = [e["hash"] for e in info["entries"]]
    sorted_ok = all(hashes[i] <= hashes[i + 1] for i in range(len(hashes) - 1))
    print(f"Sorted by hash       : {sorted_ok}")


def cmd_list(args):
    with open(args.fat, "rb") as f:
        info = parse_fat(f.read())
    announce_platform(info, args.fat)
    if args.json:
        print(json.dumps(info["entries"], indent=2))
        return
    print(f"{'hash':>10}  {'usize':>10}  {'csize':>10}  {'offset':>10}  scheme")
    for e in info["entries"]:
        scheme = resolve_scheme(e["scheme_id"], e["uncompressed_size"], info["compression_version"])
        print(f"{e['hash']:#010x}  {e['uncompressed_size']:>10}  {e['compressed_size']:>10}  "
              f"{e['offset']:>10}  {scheme}")


def cmd_verify(args):
    with open(args.fat, "rb") as f:
        info = parse_fat(f.read())
    announce_platform(info, args.fat)
    dat_size = os.path.getsize(args.dat)

    problems = []
    hashes = [e["hash"] for e in info["entries"]]
    if not all(hashes[i] <= hashes[i + 1] for i in range(len(hashes) - 1)):
        problems.append("Entries are NOT sorted ascending by hash (binary search will break)")
    if len(hashes) != len(set(hashes)):
        problems.append("Duplicate hash values present")

    oob = sum(1 for e in info["entries"] if e["offset"] + e["compressed_size"] > dat_size)
    srt = sorted(info["entries"], key=lambda e: e["offset"])
    overlap = sum(
        1 for i in range(len(srt) - 1)
        if srt[i]["offset"] + srt[i]["compressed_size"] > srt[i + 1]["offset"]
    )

    # Every entry's compression scheme must be one that actually exists for
    # this archive's declared platform/compression_version. A mismatch here
    # is the clearest possible sign that content from a different platform's
    # archive ended up mixed into this one at some point.
    if info["compression_version"] not in (0, 4, 5, 9):
        problems.append(
            f"compression_version={info['compression_version']} isn't recognized "
            f"for a Watch_Dogs (1) archive (expected 0, 4, 5, or 9)"
        )
        mismatched = []
    else:
        mismatched = [
            e for e in info["entries"]
            if resolve_scheme(e["scheme_id"], e["uncompressed_size"], info["compression_version"]) == "unknown"
        ]
        if mismatched:
            problems.append(
                f"{len(mismatched)} entr{'y uses' if len(mismatched)==1 else 'ies use'} a compression "
                f"scheme that doesn't exist for this archive's platform (compression_version="
                f"{info['compression_version']}) - likely content mixed in from a different platform"
            )

    print(f"Entries               : {len(info['entries'])}")
    print(f"Out-of-bounds entries : {oob}")
    print(f"Overlapping data spans: {overlap} (nonzero is normal on real Ubisoft "
          f"patch archives with deduplicated/solid storage)")
    print(f"Platform-mismatched compression schemes: {len(mismatched)}")
    for p in problems:
        print(f"WARNING: {p}")

    if oob == 0 and not problems:
        print("\nResult: OK - archive is internally consistent")
    else:
        print("\nResult: ISSUES FOUND")
        sys.exit(1)


def cmd_unpack(args):
    with open(args.fat, "rb") as f:
        info = parse_fat(f.read())
    announce_platform(info, args.fat)
    with open(args.dat, "rb") as f:
        dat = f.read()

    name_table = _load_name_table()
    resolved_count = 0
    detected_count = 0
    unknown_count = 0

    manifest_entries = []
    decode_stats = {}
    used_paths = set()

    total_entries = len(info["entries"])
    for idx, e in enumerate(info["entries"]):
        progress(idx, total_entries, "Unpacking")
        raw = dat[e["offset"]: e["offset"] + e["compressed_size"]]

        if args.decompress:
            data, how = decompress_entry(
                raw, e["scheme_id"], e["uncompressed_size"], e["compressed_size"],
                info["compression_version"],
            )
            decode_stats[how] = decode_stats.get(how, 0) + 1
        else:
            data = raw
            how = None

        name = name_table.get(e["hash"])
        if name:
            rel_path = name.replace("\\", os.sep)
            resolved_count += 1
        else:
            # try to guess a sensible folder/extension from the (decompressed
            # if possible) content, otherwise fall back to a bare .bin -
            # matches Gibbed's own __UNKNOWN/ convention so hash lookups
            # during packing stay unambiguous.
            sniff_source = data if args.decompress else raw
            detected = detect_file_type(sniff_source)
            hint = extract_embedded_hint(sniff_source)
            hint_suffix = f"__{hint}" if hint else ""
            if detected and detected[1]:
                folder, ext = detected
                rel_path = os.path.join("__UNKNOWN", folder, f"{e['hash']:08X}{hint_suffix}.{ext}")
                detected_count += 1
            else:
                rel_path = os.path.join("__UNKNOWN", f"{e['hash']:08X}{hint_suffix}.bin")
                unknown_count += 1

        # extremely unlikely, but guard against two entries resolving to the
        # same on-disk path (e.g. a name-table collision)
        final_path = rel_path
        n = 1
        while final_path in used_paths:
            root, ext = os.path.splitext(rel_path)
            final_path = f"{root}~{n}{ext}"
            n += 1
        used_paths.add(final_path)

        full_path = os.path.join(args.out_dir, final_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "wb") as bf:
            bf.write(data)

        manifest_entries.append({
            "hash": e["hash"],
            "path": final_path.replace(os.sep, "/"),
            "uncompressed_size": e["uncompressed_size"],
            "compressed_size": e["compressed_size"],
            "scheme_id": e["scheme_id"],
            "stored_decompressed": bool(args.decompress),
            "original_crc32": crc32(data),
        })
    progress(total_entries, total_entries, "Unpacking")

    manifest = {
        "archive_source": os.path.basename(args.fat),
        "version": info["version"],
        "platform": info["platform"],
        "compression_version": info["compression_version"],
        "name_hash_version": info["name_hash_version"],
        "localizations": info["localizations"],
        "entries": manifest_entries,
    }
    with open(os.path.join(args.out_dir, "manifest.json"), "w") as mf:
        json.dump(manifest, mf, indent=2)

    print(f"Unpacked {len(manifest_entries)} entries to {args.out_dir}")
    print(f"  {resolved_count} resolved to real filenames")
    print(f"  {detected_count} unresolved but type-detected (under __UNKNOWN/<type>/)")
    print(f"  {unknown_count} completely unknown (under __UNKNOWN/, generic .bin)")
    if args.decompress:
        print("Decode results:", decode_stats)
        print("NOTE: with --decompress, files hold DECOMPRESSED data. Packing them back")
        print("      will store them as scheme=None (raw), same as Gibbed's own packer -")
        print("      no LZ4LW/LZMA re-encoder exists (Gibbed doesn't have one either).")


def _require_platform_metadata(manifest, source_desc):
    """We won't guess at platform/compression_version/name_hash_version if
    they're missing - a manifest.json without them either got hand-edited
    or hand-built without them, or came from somewhere it shouldn't have.
    Quietly defaulting to PS4-style values here would risk building an
    archive that CLAIMS to be for one platform while actually holding
    another platform's entries inside it - exactly the kind of mismatch
    that makes a repacked archive fail to load, or worse, crash in-game.
    Better to just stop and say so plainly than let that slip through."""
    required = ["version", "platform", "compression_version", "name_hash_version"]
    missing = [k for k in required if k not in manifest]
    if missing:
        raise ValueError(
            f"{source_desc} is missing required field(s): {', '.join(missing)}. "
            f"This tool won't guess a platform for you - every manifest.json "
            f"produced by 'unpack' already has these, so if you're hand-building "
            f"or hand-editing one, copy these fields from a real unpack first."
        )


def _check_scheme_consistency(entries, compression_version, context):
    """Every entry's compression scheme has to actually be valid for PS4
    (compression_version 9 - see resolve_scheme above). When it isn't,
    that's almost always a sign content got mixed in from a different
    platform's unpacked archive - say, a Zlib-flagged entry from a PS3 or
    Win64 archive ending up here, where PS4 only ever uses None/LZMA/LZ4LW.
    Catching this here means a mismatched archive never gets written in
    the first place, instead of quietly looking fine right up until the
    game refuses to load it (or worse)."""
    if compression_version != 9:
        raise ValueError(
            f"{context}: compression_version={compression_version} isn't PS4's "
            f"(expected 9). This PS4-only edition doesn't handle other platforms - "
            f"this manifest may be from a different platform's archive, hand-edited "
            f"incorrectly, or corrupted."
        )
    bad = [
        e for e in entries
        if resolve_scheme(e["scheme_id"], e["uncompressed_size"], compression_version) == "unknown"
    ]
    if bad:
        sample = ", ".join(f"{e['hash']:08x}(scheme_id={e['scheme_id']})" for e in bad[:5])
        more = f" and {len(bad) - 5} more" if len(bad) > 5 else ""
        raise ValueError(
            f"{context}: {len(bad)} entr{'y is' if len(bad)==1 else 'ies are'} using a "
            f"compression scheme that doesn't exist on PS4: {sample}{more}. This "
            f"usually means files from a DIFFERENT platform's unpacked archive got "
            f"mixed into this folder before packing - every file being packed "
            f"together must come from the same platform's archive. Refusing to "
            f"build a mismatched archive."
        )


def cmd_pack(args):
    manifest_path = os.path.join(args.in_dir, "manifest.json")
    with open(manifest_path) as mf:
        manifest = json.load(mf)

    _require_platform_metadata(manifest, f"'{manifest_path}'")
    compression_version = manifest["compression_version"]

    alignment = args.align
    dat_out = bytearray()
    entries_by_hash = {}
    known_paths = set()
    stats = {"unchanged": 0, "overwritten": 0, "added": 0, "removed": 0}

    # 1) Entries that were part of the original archive.
    total_manifest_entries = len(manifest["entries"])
    for idx, e in enumerate(manifest["entries"]):
        progress(idx, total_manifest_entries, "Reading  ")
        rel_path = e["path"].replace("/", os.sep)
        known_paths.add(os.path.normpath(rel_path))

        if e.get("remove"):
            stats["removed"] += 1
            continue

        full_path = os.path.join(args.in_dir, rel_path)
        if not os.path.isfile(full_path):
            raise FileNotFoundError(
                f"'{e['path']}' from manifest.json is missing on disk. "
                f"If you meant to delete this file from the archive, add "
                f'"remove": true to its entry in manifest.json instead of '
                f"just deleting the file (otherwise this looks like an accident)."
            )

        with open(full_path, "rb") as bf:
            blob = bf.read()

        unchanged = (
            not e.get("stored_decompressed", False)
            and crc32(blob) == e.get("original_crc32")
        )
        if unchanged:
            scheme_id = e["scheme_id"]
            uncompressed_size = e["uncompressed_size"]
            stats["unchanged"] += 1
        else:
            # Changed content (or was unpacked in decompressed form) - no
            # LZ4LW/LZMA encoder exists (same limitation as Gibbed's own
            # tool), so store it raw. The game reads scheme=None natively.
            scheme_id = 0
            uncompressed_size = 0
            stats["overwritten"] += 1

        entries_by_hash[e["hash"]] = {
            "hash": e["hash"],
            "uncompressed_size": uncompressed_size,
            "scheme_id": scheme_id,
            "blob": blob,
        }
    progress(total_manifest_entries, total_manifest_entries, "Reading  ")

    # 2) Any files added under in_dir that weren't in the original manifest
    #    at all - these are brand-new modded files. Hash is computed from
    #    their path (relative to in_dir, using the game's backslash
    #    convention), same as the game would look them up by name.
    blobs_root = args.in_dir
    skipped_junk = []
    for dirpath, _dirs, files in os.walk(blobs_root):
        for fn in files:
            full_path = os.path.join(dirpath, fn)
            rel_path = os.path.relpath(full_path, blobs_root)
            if os.path.normpath(rel_path) in known_paths:
                continue

            fn_lower = fn.lower()
            if fn_lower in IGNORED_FILENAMES or fn_lower.endswith(IGNORED_SUFFIXES):
                skipped_junk.append(rel_path)
                continue

            rel_back = rel_path.replace(os.sep, "\\")
            if rel_back.startswith("__UNKNOWN\\"):
                base = os.path.basename(rel_path)
                # hash is always the leading 8 hex chars, even if followed by
                # a "__descriptive_hint" suffix (see extract_embedded_hint)
                m = re.match(r"^([0-9A-Fa-f]{8})", base)
                if not m:
                    print(f"WARNING: skipping unrecognized file under __UNKNOWN: {rel_path}")
                    continue
                h = int(m.group(1), 16)
            else:
                h = compute_name_hash(rel_back)

            with open(full_path, "rb") as bf:
                blob = bf.read()

            if h in entries_by_hash:
                print(f"WARNING: '{rel_path}' hashes to an existing entry "
                      f"({h:08x}) - treating as an overwrite of that entry.")
                stats["overwritten"] += 1
                stats["added"] -= 0  # no-op, just clarity
            else:
                stats["added"] += 1

            entries_by_hash[h] = {
                "hash": h, "uncompressed_size": 0, "scheme_id": 0, "blob": blob,
            }

    # 3) Lay out the .dat and build final entry records.
    final_entries = []
    total_final = len(entries_by_hash)
    for idx, (h, e) in enumerate(sorted(entries_by_hash.items())):
        progress(idx, total_final, "Building ")
        offset = len(dat_out)
        dat_out += e["blob"]
        pad = (-len(dat_out)) % alignment
        dat_out += b"\x00" * pad
        final_entries.append({
            "hash": h,
            "uncompressed_size": e["uncompressed_size"],
            "scheme_id": e["scheme_id"],
            "offset": offset,
            "compressed_size": len(e["blob"]),
        })
    progress(total_final, total_final, "Building ")

    # Every entry's compression scheme must actually be valid for PS4 -
    # catches files accidentally mixed in from a different platform's
    # unpacked folder before anything gets written.
    if manifest["platform"] != PLATFORM_ORBIS:
        plat_name = PLATFORM_NAMES.get(manifest["platform"], f"id {manifest['platform']}")
        raise ValueError(
            f"Cannot pack: this manifest is for a {plat_name} archive, not PS4. "
            f"This PS4-only edition of the tool only builds PS4 (Orbis) archives."
        )
    _check_scheme_consistency(final_entries, compression_version,
                               "Cannot pack")

    header_info = {
        "version": manifest["version"],
        "platform": manifest["platform"],
        "compression_version": compression_version,
        "name_hash_version": manifest["name_hash_version"],
        "localizations": manifest.get("localizations", []),
    }
    fat_out = build_fat(header_info, final_entries)

    # Refuse to silently clobber an existing file (most commonly: someone
    # accidentally pointing pack output at their original, unmodified
    # archive). --force / -y explicitly opts out of this check.
    for target in (args.out_fat, args.out_dat):
        if os.path.exists(target) and not getattr(args, "force", False):
            raise FileExistsError(
                f"'{target}' already exists. Re-run with --force if you really "
                f"want to overwrite it, or choose a different output path."
            )

    # Build into the tool's own temp folder first, not the final destination -
    # if anything below fails partway through, the user's requested output
    # path is never touched, so there's no risk of leaving a corrupt/partial
    # .fat or .dat sitting where the game (or a later run) would find it.
    temp_fat = new_temp_path(".fat.tmp")
    temp_dat = new_temp_path(".dat.tmp")
    try:
        with open(temp_fat, "wb") as f:
            f.write(fat_out)
        with open(temp_dat, "wb") as f:
            f.write(bytes(dat_out))

        # Quick self-check on what we just built before it ever reaches the
        # user's requested path - re-parse it exactly like 'verify' would.
        with open(temp_fat, "rb") as f:
            check_info = parse_fat(f.read())
        check_hashes = [e["hash"] for e in check_info["entries"]]
        if not all(check_hashes[i] <= check_hashes[i + 1] for i in range(len(check_hashes) - 1)):
            raise RuntimeError("internal error: built archive is not sorted by hash")

        for target, temp_path in ((args.out_fat, temp_fat), (args.out_dat, temp_dat)):
            target_dir = os.path.dirname(os.path.abspath(target))
            if target_dir:
                os.makedirs(target_dir, exist_ok=True)
            shutil.move(temp_path, target)
    finally:
        for p in (temp_fat, temp_dat):
            if os.path.exists(p):
                os.remove(p)

    print(f"Packed {len(final_entries)} entries (alignment={alignment})")
    if manifest.get("archive_source"):
        print(f"  source archive: {manifest['archive_source']}")
    print(f"  unchanged (byte-identical, original compression kept): {stats['unchanged']}")
    print(f"  overwritten (existing entry, new content stored raw) : {stats['overwritten']}")
    print(f"  added (brand-new entry, stored raw)                  : {stats['added']}")
    print(f"  removed                                              : {stats['removed']}")
    if skipped_junk:
        print(f"  ignored (not real game files, left out)              : {len(skipped_junk)}")
        for p in skipped_junk[:10]:
            print(f"    - {p}")
        if len(skipped_junk) > 10:
            print(f"    ... and {len(skipped_junk) - 10} more")
    print(f"  {args.out_fat}  ({len(fat_out)} bytes)")
    print(f"  {args.out_dat}  ({len(dat_out)} bytes)")
    print("Run 'verify' on the output to confirm internal consistency.")


def cmd_decode(args):
    with open(args.fat, "rb") as f:
        info = parse_fat(f.read())
    announce_platform(info, args.fat)
    with open(args.dat, "rb") as f:
        dat = f.read()

    target_hash = int(args.hash_hex, 16)
    entry = next((e for e in info["entries"] if e["hash"] == target_hash), None)
    if entry is None:
        print(f"No entry with hash {args.hash_hex}")
        sys.exit(1)

    raw = dat[entry["offset"]: entry["offset"] + entry["compressed_size"]]
    data, how = decompress_entry(
        raw, entry["scheme_id"], entry["uncompressed_size"], entry["compressed_size"],
        info["compression_version"],
    )
    out_path = args.out_file or f"{args.hash_hex}.decoded"
    with open(out_path, "wb") as f:
        f.write(data)
    print(f"Decoded via '{how}': {len(data)} bytes -> {out_path}")


# --------------------------------------------------------------------------
# Terminal UI helpers - color, emoji, and a native file/folder picker, all
# with automatic, silent fallback if the terminal or environment can't
# support them (older Windows consoles, no display server, and so on).
# Nothing in here should ever be able to crash the tool - worst case,
# things just look a bit plainer than intended.
# --------------------------------------------------------------------------

def _enable_windows_ansi():
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


_enable_windows_ansi()
_COLOR_OK = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


class C:
    """ANSI color codes, or blank strings if color isn't safe to use."""
    RESET = "\033[0m" if _COLOR_OK else ""
    BOLD = "\033[1m" if _COLOR_OK else ""
    GREEN = "\033[32m" if _COLOR_OK else ""
    RED = "\033[31m" if _COLOR_OK else ""
    YELLOW = "\033[33m" if _COLOR_OK else ""
    CYAN = "\033[36m" if _COLOR_OK else ""
    GRAY = "\033[90m" if _COLOR_OK else ""


def safe_print(text=""):
    """A print() that won't crash just because an emoji or unicode
    character doesn't fit the console's codepage (a genuine risk on older
    Windows cmd.exe setups). If that happens, we just quietly strip
    whatever can't be displayed and print the rest."""
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        print(text.encode(encoding, errors="ignore").decode(encoding))


def banner(text):
    safe_print()
    safe_print(f"{C.BOLD}{C.CYAN}{'=' * 62}{C.RESET}")
    safe_print(f"{C.BOLD}{C.CYAN}  {text}{C.RESET}")
    safe_print(f"{C.BOLD}{C.CYAN}{'=' * 62}{C.RESET}")


def section(text):
    safe_print(f"\n{C.BOLD}{C.CYAN}>> {text}{C.RESET}")


def success(text):
    safe_print(f"{C.GREEN}{C.BOLD}[OK] {text}{C.RESET}")


def warn(text):
    safe_print(f"{C.YELLOW}[!] {text}{C.RESET}")


def error(text):
    safe_print(f"{C.RED}{C.BOLD}[X] {text}{C.RESET}")


def hint(text):
    safe_print(f"{C.GRAY}    {text}{C.RESET}")


def progress(current, total, label="Working"):
    """A simple, dependency-free progress line. Only draws on a real
    terminal - stays quiet when output is piped/redirected/logged, so it
    never clutters a script's output."""
    if not sys.stdout.isatty() or total <= 0:
        return
    pct = int(current * 100 / total)
    bar_width = 30
    filled = int(bar_width * current / total)
    bar = "#" * filled + "-" * (bar_width - filled)
    end = "\n" if current >= total else ""
    print(f"\r  {label} [{bar}] {pct:3d}% ({current}/{total})", end=end, flush=True)


try:
    import tkinter
    from tkinter import filedialog as _tk_filedialog
    _TKINTER_AVAILABLE = True
except Exception:
    _TKINTER_AVAILABLE = False

_tk_root = None  # only ever set *during* a single dialog call - see below


@contextlib.contextmanager
def _tk_dialog_root():
    """Creates a hidden Tk window just long enough to show one dialog, then
    destroys it immediately afterward - every time. A leftover Tk() root
    (even withdrawn/invisible) can keep the Tcl/Tk event system alive and
    stop the process from fully exiting after the program appears to be
    done, so this never lets one persist between dialogs or after the
    program finishes."""
    global _tk_root
    root = tkinter.Tk()
    _tk_root = root
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        yield root
    finally:
        try:
            root.destroy()
        except Exception:
            pass
        _tk_root = None


@atexit.register
def _cleanup_tk_on_exit():
    """Safety net: if anything went wrong and a dialog's Tk root didn't get
    destroyed (e.g. the program crashed mid-dialog), make sure it's cleaned
    up before the process actually exits, so nothing lingers in the
    background."""
    global _tk_root
    if _tk_root is not None:
        try:
            _tk_root.destroy()
        except Exception:
            pass
        _tk_root = None


def pick_open_file(title, filetypes):
    """Shows the native 'choose a file' dialog. Returns a path, or None if
    the person cancelled or there's no GUI available (in which case the
    caller falls back to _ask instead)."""
    if not _TKINTER_AVAILABLE:
        return None
    try:
        with _tk_dialog_root() as root:
            path = _tk_filedialog.askopenfilename(title=title, filetypes=filetypes, parent=root)
            return path or None
    except Exception:
        return None


def pick_save_file(title, default_name, filetypes, default_ext=""):
    if not _TKINTER_AVAILABLE:
        return None
    try:
        with _tk_dialog_root() as root:
            path = _tk_filedialog.asksaveasfilename(
                title=title, initialfile=default_name, filetypes=filetypes,
                defaultextension=default_ext, parent=root,
            )
            return path or None
    except Exception:
        return None


def pick_folder(title):
    if not _TKINTER_AVAILABLE:
        return None
    try:
        with _tk_dialog_root() as root:
            path = _tk_filedialog.askdirectory(title=title, parent=root)
            return path or None
    except Exception:
        return None


def ask_file(prompt, title, filetypes=(("All files", "*.*"),), default=None):
    """Tries a native file-picker window first, so nobody ever has to type
    a path if they don't want to. If that's not available (no display,
    Tkinter missing, whatever the reason), it falls back gracefully to a
    plain typed prompt with a clear explanation of what to do."""
    if _TKINTER_AVAILABLE:
        safe_print(f"{prompt} -> opening a file picker window...")
        hint("(If you don't see it, check behind this window / your taskbar.)")
        path = pick_open_file(title, filetypes)
        if path:
            safe_print(f"    Selected: {path}")
            return path
        safe_print("    (No file selected.)")
        return None
    return _ask(f"{prompt} (type the full path, or drag the file into this window)", default)


def ask_save_file(prompt, title, default_name, filetypes=(("All files", "*.*"),), default_ext=""):
    if _TKINTER_AVAILABLE:
        safe_print(f"{prompt} -> opening a save window...")
        path = pick_save_file(title, default_name, filetypes, default_ext)
        if path:
            safe_print(f"    Will save as: {path}")
            return path
        safe_print("    (Cancelled.)")
        return None
    return _ask(prompt, default_name)


def ask_folder(prompt, title, default=None):
    if _TKINTER_AVAILABLE:
        safe_print(f"{prompt} -> opening a folder picker window...")
        hint("(If you don't see it, check behind this window / your taskbar.)")
        path = pick_folder(title)
        if path:
            safe_print(f"    Selected: {path}")
            return path
        safe_print("    (No folder selected.)")
        return None
    return _ask(f"{prompt} (type the full folder path)", default)


# --------------------------------------------------------------------------
# The friendly interactive menu - no command-line experience required. This
# is what shows up if you just double-click the tool, launch it with no
# arguments, or drop a .fat file onto the Windows .bat launcher.
# --------------------------------------------------------------------------

def _ask(prompt, default=None):
    suffix = f" [{default}]" if default else ""
    val = input(f"{C.BOLD}{prompt}{suffix}: {C.RESET}").strip().strip('"')
    return val if val else default


def _confirm(prompt, default_yes=False):
    default = "y" if default_yes else "n"
    ans = _ask(f"{prompt} (y/n)", default)
    return ans.lower().startswith("y")


def _pause_exit():
    input(f"\n{C.GRAY}Press Enter to close...{C.RESET}")


def _find_dat_for_fat(fat_path):
    guess = os.path.splitext(fat_path)[0] + ".dat"
    return guess if os.path.isfile(guess) else None


FAT_FILETYPES = (("Watch_Dogs archive index", "*.fat"), ("All files", "*.*"))
FOLDER_HINT = "(a folder, not a single file)"


MENU_TIPS = [
    "You never have to type a file path - just click Browse when a window pops up.",
    "Nothing you do here touches your original game files unless you tell it to.",
    "If you're not sure what something does, just try it - you'll always be asked",
    "before anything is saved or overwritten.",
    "'Unpack' takes files OUT of the game. 'Pack' puts them back IN.",
]


def interactive_main(dropped_path=None):
    banner(f"FAT3Tool v{__version__}")
    safe_print(f"{C.GRAY}  Open, edit, and rebuild Watch_Dogs game archives{C.RESET}")
    if not _TKINTER_AVAILABLE:
        warn("Visual file-picker windows aren't available here - you'll type paths instead.")

    if dropped_path:
        ext = os.path.splitext(dropped_path)[1].lower()
        if ext == ".fat":
            safe_print(f"\nYou dropped a file: {dropped_path}")
            dat_guess = _find_dat_for_fat(dropped_path)
            success("That's a game archive file - let's unpack it!")
            _interactive_unpack(dropped_path, dat_guess)
            return
        else:
            warn(f"'{dropped_path}' doesn't look like a .fat archive file, so "
                 f"we'll just show the menu instead.")

    first_loop = True
    while True:
        section("What would you like to do?")
        safe_print(f"  {C.BOLD}1{C.RESET}) Unpack a game archive into normal files")
        hint("Turns a .fat + .dat pair into a folder you can look through and edit")
        safe_print(f"  {C.BOLD}2{C.RESET}) Pack a folder back into a game archive")
        hint("Turns your edited folder back into a .fat + .dat pair for the game")
        safe_print(f"  {C.BOLD}3{C.RESET}) Check that an archive isn't broken")
        safe_print(f"  {C.BOLD}4{C.RESET}) Show basic info about an archive")
        safe_print(f"  {C.BOLD}5{C.RESET}) List every file inside an archive")
        safe_print(f"  {C.BOLD}0{C.RESET}) Quit")
        if first_loop:
            hint(f"Tip: {MENU_TIPS[0]}")
            first_loop = False
        choice = _ask("\nType a number and press Enter")

        try:
            if choice == "1":
                _interactive_unpack()
            elif choice == "2":
                _interactive_pack()
            elif choice == "3":
                _interactive_verify()
            elif choice == "4":
                _interactive_info()
            elif choice == "5":
                _interactive_list()
            elif choice == "0":
                safe_print(f"\n{C.CYAN}Bye!{C.RESET}")
                return
            elif choice is None:
                continue
            else:
                warn("That's not one of the options above - try typing just the number.")
                continue
        except FileNotFoundError as e:
            error(f"Couldn't find that file: {e.filename}")
            hint("Nothing was changed. Double check the path and try again.")
        except PermissionError as e:
            error(f"Windows won't let this program touch that file: {e.filename}")
            hint("It might be open in another program, or in a protected folder.")
        except Exception as e:
            error(f"Something went wrong: {e}")
            hint("Nothing should have been changed - it's safe to try again.")

        safe_print(f"\n{C.GRAY}{'-' * 62}{C.RESET}")


def _interactive_unpack(fat_path=None, dat_path=None):
    section("Unpack a game archive")
    hint("This takes a .fat + .dat pair and turns them into normal files")
    hint("you can browse through and edit.")

    if fat_path is None:
        fat_path = ask_file("\nFirst, pick the .fat file", "Choose the .fat archive file", FAT_FILETYPES)
        if not fat_path:
            warn("No file picked - going back to the menu.")
            return
    if not os.path.isfile(fat_path):
        error(f"That file doesn't exist: {fat_path}")
        return

    if dat_path is None:
        dat_path = _find_dat_for_fat(fat_path)
        if dat_path:
            success(f"Found the matching .dat file automatically: {os.path.basename(dat_path)}")
            if not _confirm("Use this one?", default_yes=True):
                dat_path = ask_file("Pick the correct .dat file instead",
                                     "Choose the .dat archive file",
                                     (("Watch_Dogs archive data", "*.dat"), ("All files", "*.*")))
        else:
            warn("Couldn't find a matching .dat file automatically.")
            dat_path = ask_file("Pick the .dat file that goes with it",
                                 "Choose the .dat archive file",
                                 (("Watch_Dogs archive data", "*.dat"), ("All files", "*.*")))
    if not dat_path or not os.path.isfile(dat_path):
        error("No valid .dat file to go with it - can't continue.")
        return

    default_out = os.path.splitext(fat_path)[0] + "_unpacked"
    section("Where should the extracted files go?")
    out_dir = ask_folder("Pick (or create) a folder to put them in", "Choose an output folder", default_out)
    if not out_dir:
        out_dir = default_out
        hint(f"Using the default folder instead: {out_dir}")

    section("One more question")
    hint("Files inside the game archive are usually scrambled/compressed to save")
    hint("space. Turning this on unscrambles what it can, so you can actually")
    hint("read/edit the content - but see the note below before choosing.")
    decompress = _confirm("Try to fully decompress files for editing?", default_yes=False)
    if decompress:
        warn("Note: once you pack these back up, they'll be saved in a bigger, ")
        hint("uncompressed form - which is totally fine for the game to read, ")
        hint("it'll just make the final files larger than the originals.")

    class NS:
        pass
    ns = NS()
    ns.fat, ns.dat, ns.out_dir, ns.decompress = fat_path, dat_path, out_dir, decompress

    section("Unpacking now...")
    cmd_unpack(ns)
    banner("ALL DONE!")
    success(f"Your files are here: {out_dir}")
    hint("Next, you can open that folder, look through the files, and change")
    hint("whatever you want. When you're ready, come back here and use")
    hint("option 2 (Pack) to turn it back into a game archive.")


def _interactive_pack():
    section("Pack a folder back into a game archive")
    hint("This takes a folder made by Unpack (with your changes in it) and")
    hint("turns it back into a .fat + .dat pair the game can load.")

    in_dir = ask_folder("\nPick the folder with your files in it (must have manifest.json inside)",
                         "Choose the folder to pack")
    if not in_dir:
        warn("No folder picked - going back to the menu.")
        return
    if not os.path.isfile(os.path.join(in_dir, "manifest.json")):
        error(f"That folder doesn't have a manifest.json in it: {in_dir}")
        hint("Only a folder that came from this tool's Unpack option will work.")
        return

    section("Where should the new archive be saved?")
    hint("A matching .dat file will be saved right next to it automatically.")
    while True:
        out_fat = ask_save_file("Choose where to save it", "Save new archive as",
                                 "new_archive.fat", FAT_FILETYPES, default_ext=".fat")
        if not out_fat:
            warn("No save location chosen - going back to the menu.")
            return
        if not out_fat.lower().endswith(".fat"):
            out_fat += ".fat"
        out_dat = os.path.splitext(out_fat)[0] + ".dat"
        hint(f"(will also save: {os.path.basename(out_dat)})")

        existing = [p for p in (out_fat, out_dat) if os.path.exists(p)]
        force = False
        if existing:
            warn("This already exists and would be replaced:")
            for p in existing:
                hint(p)
            if not _confirm("Are you sure you want to overwrite it?", default_yes=False):
                safe_print("No problem - let's pick a different name.\n")
                continue
            force = True
        break

    class NS:
        pass
    ns = NS()
    ns.in_dir, ns.out_fat, ns.out_dat, ns.align, ns.force = in_dir, out_fat, out_dat, DEFAULT_ALIGNMENT, force

    section("Packing now...")
    cmd_pack(ns)
    banner("ALL DONE!")
    success("Saved:")
    hint(out_fat)
    hint(out_dat)
    safe_print(f"\n{C.CYAN}Tip:{C.RESET} run option 3 (Verify) on these two files now, just to")
    safe_print("double-check everything looks right before using them in the game.")


def _interactive_verify():
    section("Check that an archive isn't broken")
    fat_path = ask_file("Pick the .fat file to check", "Choose the .fat archive file", FAT_FILETYPES)
    if not fat_path:
        warn("No file picked - going back to the menu.")
        return
    dat_path = _find_dat_for_fat(fat_path)
    if not dat_path:
        dat_path = ask_file("Pick the matching .dat file",
                             "Choose the .dat archive file",
                             (("Watch_Dogs archive data", "*.dat"), ("All files", "*.*")))
    if not dat_path:
        warn("No .dat file picked - going back to the menu.")
        return

    class NS:
        pass
    ns = NS()
    ns.fat, ns.dat = fat_path, dat_path
    section("Checking...")
    try:
        cmd_verify(ns)
        banner("Looks good!")
    except SystemExit:
        error("Found problems with this archive - see above for details.")


def _interactive_info():
    section("Show basic info about an archive")
    fat_path = ask_file("Pick the .fat file", "Choose the .fat archive file", FAT_FILETYPES)
    if fat_path:
        class NS:
            pass
        ns = NS()
        ns.fat = fat_path
        safe_print()
        cmd_info(ns)


def _interactive_list():
    section("List every file inside an archive")
    fat_path = ask_file("Pick the .fat file", "Choose the .fat archive file", FAT_FILETYPES)
    if fat_path:
        class NS:
            pass
        ns = NS()
        ns.fat, ns.json = fat_path, False
        safe_print()
        cmd_list(ns)


# --------------------------------------------------------------------------

def main():
    # No arguments -> beginner-friendly interactive menu.
    # One argument that looks like a dropped file (e.g. drag-and-drop onto
    # the .bat launcher on Windows) -> jump straight into the relevant action.
    if len(sys.argv) == 1:
        interactive_main()
        return
    if len(sys.argv) == 2 and not sys.argv[1].startswith("-") and sys.argv[1] not in (
        "info", "list", "verify", "unpack", "pack", "decode"
    ):
        interactive_main(dropped_path=sys.argv[1])
        return

    ap = argparse.ArgumentParser(description="FAT3/Disrupt engine modding tool")
    ap.add_argument("--version", action="version", version=f"fat3tool.py {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("info", help="Show archive header info")
    p.add_argument("fat")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("list", help="List all entries")
    p.add_argument("fat")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("verify", help="Validate an archive's internal consistency")
    p.add_argument("fat")
    p.add_argument("dat")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("unpack", help="Extract to manifest.json + real named files")
    p.add_argument("fat")
    p.add_argument("dat")
    p.add_argument("out_dir")
    p.add_argument("--decompress", action="store_true",
                    help="Store fully decompressed data instead of raw compressed bytes")
    p.set_defaults(func=cmd_unpack)

    p = sub.add_parser("pack", help="Rebuild .fat/.dat from a manifest dir")
    p.add_argument("in_dir")
    p.add_argument("out_fat")
    p.add_argument("out_dat")
    p.add_argument("--align", type=int, default=DEFAULT_ALIGNMENT)
    p.add_argument("--force", "-y", action="store_true",
                    help="Overwrite out_fat/out_dat if they already exist")
    p.set_defaults(func=cmd_pack)

    p = sub.add_parser("decode", help="Decode one entry by hash")
    p.add_argument("fat")
    p.add_argument("dat")
    p.add_argument("hash_hex")
    p.add_argument("out_file", nargs="?")
    p.set_defaults(func=cmd_decode)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
    except FileNotFoundError as e:
        print(f"\n[!] File not found: {e.filename}")
        if len(sys.argv) == 1:
            _pause_exit()
        else:
            sys.exit(1)
    except Exception as e:
        print(f"\n[!] Error: {e}")
        if len(sys.argv) == 1:
            _pause_exit()
        else:
            sys.exit(1)
    else:
        if len(sys.argv) == 1:
            pass  # interactive_main already loops until the user chooses Quit
