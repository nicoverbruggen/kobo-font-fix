This repo processes TrueType fonts for Kobo e-readers. Read `README.md` for full user-facing details; this file is the short operational guide for agents.

## Using font tooling with Python

- Don't use the built-in version of Python and assorted font tooling unless specified. Instead, prefer the use the [OCI image](https://github.com/nicoverbruggen/fntbld-oci) as documented below, as it avoids needing to mess with custom Python environments and such.

- This OCI image can be run with Docker, Podman, OrbStack, or any other compatible container runtime. The `ghcr.io/nicoverbruggen/fntbld-oci:latest` image includes the native font tooling this project needs.

- Example container command:

  ```bash
  docker run --rm \
    -v "$PWD:/work" \
    -w /work \
    ghcr.io/nicoverbruggen/fntbld-oci:latest \
    python3 -m unittest discover -s tests -v
  ```

- Podman and other Docker-compatible CLIs work with the same mount and working directory arguments:

  ```bash
  podman run --rm \
    -v "$PWD:/work" \
    -w /work \
    ghcr.io/nicoverbruggen/fntbld-oci:latest \
    python3 -m unittest discover -s tests -v
  ```

- Do not assume the system Python or Codex bundled Python has FontTools or the native font binaries installed. Local runs require Python 3, FontTools, and `ots-sanitize`; depending on enabled operations they may also require `font-line`, `skia-pathops`, and `ttfautohint`.

- The KF preset does not use `ttfautohint`.

## Main Script Behavior

- If you are told to actually run `kobofix.py`, you should know that it writes processed fonts next to the input fonts. Keep backups of source fonts before running it.
- Supported font filenames must contain one of: `Regular`, `Italic`, `Bold`, or `BoldItalic`. This naming convention matters for Kobo compatibility.
- If no preset or flags are provided, the script prompts for a preset. In automated tests or agent runs, pass explicit flags or `--preset`.

## Using the KF preset

The KF preset is the Kobo-specific path and should be treated carefully.

- Applies the `KF` prefix and strips known existing prefixes such as `NV` and `KF` before applying the new prefix.
- Adds a legacy `kern` table from GPOS kerning pairs.
- Simplifies outlines, removes overlaps, cleans degenerate contours, and flattens composite glyphs unless `--outline skip` is used.
- Removes TrueType hinting from KF fonts and gives every outline glyph the same no-op TrueType instruction: `SVTCA[Y]`, opcode `0x00`. This is done to avoid needing to install [NickelHintFix](https://github.com/nicoverbruggen/NickelHintFix).
- This no-op instruction is intentional. Kobo's iType rasterizer applies automatic grid-fitting to glyphs with no per-glyph instructions, which can cause vertical wobble. A no-op per-glyph program routes iType through its interpreter while leaving the outline unchanged.

Typical KF run:

```bash
python3 kobofix.py --preset kf ./src/*.ttf
```

## Using the NV preset

Used for adding new fonts to [`ebook-fonts`](https://github.com/nicoverbruggen/ebook-fonts).

- Applies the `NV` prefix.
- Applies 20 percent line spacing by default.
- Skips kerning changes.
- Leaves outlines mostly untouched.

A typical NV run looks like this:

```bash
python3 kobofix.py --preset nv *.ttf
```

## Testing

- Full test suite:

  ```bash
  python3 -m unittest discover -s tests -v
  ```

- Prefer running that inside the documented Docker or Podman container.
- The integration tests download the latest `Readerly.zip` release into
  `./tests/fixtures` on first run, then reuse the extracted fonts.
- Generated fonts can be validated directly with:

  ```bash
  python3 validate.py ./path/to/fonts/*.ttf
  ```
