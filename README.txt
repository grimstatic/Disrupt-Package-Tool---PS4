FAT3Tool v1.0 - PS4 Edition
Watch_Dogs / Disrupt Engine Archive Tool
=========================================

A pack/unpack/verify tool for the .fat/.dat game archives used by the PS4
release of Watch_Dogs (2014), including PS4 retail - built for modding,
and built to be usable by someone who's never touched a command-line tool
in their life.

This is the stripped-down, PS4-only edition. It only understands PS4
(Orbis) archives, and will plainly refuse to open anything else rather
than risk mishandling it.

CREDITS
---------
    Cell             - for testing
    Gibbed Disrupt   - source code and cross-reference material
    Selene062398     - for LZMA and LZ4LW Orbis (PS4) decompression.

Thanks to all three - genuinely made a difference in getting this tool to
where it is. Much appreciated.

Specifically, a lot of this tool is built directly on top of Rick
"Gibbed" Gibson's own C# reference implementation (Gibbed.Disrupt).
Cross-checking against real, working source code is what turned an early,
partially-correct reverse-engineering effort into something fully
accurate.

And to the community-maintained file lists at
https://github.com/Open-Source-Modding/WatchDogs-File-Lists (a fork of
https://github.com/gibbed/WatchDogs-File-Lists), which is what lets this
tool show real, readable filenames instead of raw hash codes for almost
everything it unpacks.


HOW TO RUN IT (easiest way)
------------------------------
1. Make sure Python is installed. If you're not sure, just double-click
   "FAT3Tool.bat" - it'll tell you if Python is missing and point you to
   the download page. Takes about two minutes to install if you need it.
   During setup, make sure to tick "Add python.exe to PATH".

2. Double-click "FAT3Tool.bat".

3. A menu appears. Pick a number, follow the prompts. Any time the tool
   needs a file or folder from you, a picker window pops up - you never
   have to type a path unless you want to.

TIP: you can also drag a .fat file straight onto "FAT3Tool.bat" to jump
directly into unpacking it.


DON'T HAVE / DON'T WANT TO INSTALL PYTHON?
----------------------------------------------
If you have access to any Windows PC that already has Python, you can
build a fully standalone FAT3Tool.exe once, then copy just that one file
to any other computer - no Python needed there at all.

Double-click "Build_Standalone_EXE.bat" (only works on a PC with Python
already on it). When it's done, "FAT3Tool.exe" will be waiting in the new
"dist" folder - copy it wherever you like.

EVERY FEATURE, LISTED
------------------------

Archive support
  - Reads and writes every .fat/.dat archive Watch_Dogs (Retail) ships on
    PS4: common, patch, patch1, shaders, shadersobj, sound (+ every
    language), videos, all three DLC packs, and windy_city (+ language/
    cache variants). Same tool, same steps, no matter which one you point
    it at.

Unpacking
  - Extracts every file with its real, readable name and folder structure.
  - Anything that can't be named gets sorted into a guessed type folder
    based on its actual content (textures, scripts, audio, etc.) and,
    where possible, an extra hint pulled from readable text embedded
    inside the file itself.
  - Optional full decompression mode (--decompress) actually decodes
    PS4's LZMA and custom LZ4LW compression, so you can open and read
    file contents directly instead of just moving raw bytes.
  - Every unpack produces a manifest.json recording exactly how to put
    everything back together correctly.

Packing (rebuilding an archive)
  - Overwrite a file: just replace its contents on disk.
  - Add a brand-new file: drop it in at the path the game expects; its
    hash is computed automatically.
  - Remove a file: flag it "remove": true in manifest.json.
  - Every file you didn't touch is carried through byte-for-byte
    identical to the original, including its original compression.
  - Automatically ignores common junk (Thumbs.db, .DS_Store, desktop.ini,
    backup files) so it never accidentally gets baked in as "new content."
  - Refuses to silently guess a platform for a hand-edited or incomplete
    manifest, and refuses to build an archive if it isn't PS4, or if it
    detects files that look like they came from a different platform's
    unpacked archive.
  - Builds into its own temp folder first and only moves the finished
    result into place once everything checks out.
  - Always confirms where to save, and always asks before overwriting an
    existing file.

Checking your work
  - verify: sanity-checks an archive's internal structure and tells you
    plainly whether it's safe to use.
  - info / list: quick summaries of what's inside an archive, or a full
    listing of every entry with its size, hash, and compression scheme.
  - decode: fully decompress one specific entry from a real archive.

Made to actually be usable
  - A plain-language interactive menu - no command-line flags to
    memorize.
  - Native "Browse..." windows for picking files and folders, with an
    automatic fallback to a typed prompt if no display is available.
  - Color and clear status markers, with automatic fallback to plain text
    if your console's codepage can't display them.
  - Live progress bars for unpacking/packing large archives.
  - Clear, reassuring error messages, and nothing is ever changed or lost
    if something goes wrong or you cancel partway through.
  - Zero external dependencies - runs on plain Python, nothing to
    pip install.


A TYPICAL MODDING WORKFLOW
-----------------------------
1. Unpack your game's patch.fat / patch.dat into a folder.
2. You'll get real, readable file names and folders wherever the tool
   could figure them out, and an "__UNKNOWN" folder for anything it
   couldn't name.
3. To CHANGE a file: overwrite it in place with your new content.
   To ADD a new file: drop it in at the path the game expects it at.
   To REMOVE a file: open manifest.json, find its entry, and add
   "remove": true to it.
4. Run Pack, pointing it at that same unpacked folder, to build a new
   .fat + .dat pair.
5. Run Verify on the result before using it, just to be sure.


IMPORTANT NOTES
------------------
- The project is still under active development, issues are to be expected.
- Turning on full decompression when unpacking lets you actually read and
  edit file contents, but when you pack them back up, they're always
  stored uncompressed. There's currently no way to re-compress into PS4's
  proprietary formats - this is completely fine, the game reads
  uncompressed entries without any problem.
- Nothing here connects to the internet or touches your original game
  files unless you explicitly tell it to overwrite them.
