# Kobo Font Fix

## Overview

**`kobofix.py` is a Python script designed to process and adjust TTF fonts for Kobo e-readers for a better reading experience with the default `kepub` renderer.**

It generates a renamed font, fixes PANOSE information based on the filename, adjusts the baseline with the `font-line` utility, simplifies outlines with `skia-pathops`, flattens composite glyphs for Kobo compatibility, strips TrueType hinting from KF fonts and gives every outline glyph the same no-op TrueType instruction (so Kobo's iType rasterizer renders the raw outline instead of producing a vertical "wobble"), adds a legacy `kern` table which allows the `kepub` engine for improved rendering of kerned pairs, and validates finished output with `ots-sanitize`.

> [!NOTE]
> You can also use this to modify or fix your own, legally acquired fonts, assuming you are legally allowed to do so. The author of this script does not recommend modifying fonts which don't specify in their license agreement that they can be modified. Using this script is done at your own risk. 

## Requirements

Docker or Podman must be installed. The [`fntbld-oci`](https://github.com/nicoverbruggen/fntbld-oci/pkgs/container/fntbld-oci) container is used to build the actual fonts.

Alternatively, you can also install the dependencies and run the script locally. Python 3, FontTools, and `ots-sanitize` are always required. Depending on the enabled operations, you may also need `font-line`, `skia-pathops`, and `ttfautohint`. The KF preset does not use `ttfautohint`.

## Usage

The easiest way to run `kobofix.py` without installing the native tools yourself is to use the `fntbld-oci` image. It includes all dependencies. To use it, clone this repository, and with input fonts in `./src`, you can run a preset:

```bash
docker run --rm \
  -v "$PWD:/work" \
  -w /work \
  ghcr.io/nicoverbruggen/fntbld-oci:latest \
  python3 kobofix.py --preset kf ./src/*.ttf
```

**The script normally writes processed fonts next to the originals, but you should still keep backups before running it.**

The processed fonts are written next to the input fonts inside the mounted directory. Docker should select the correct image architecture automatically. Podman works with the same mount and working directory arguments, so if you prefer using Podman (e.g. on Linux) you can use that, too.

## How it works

### Ensuring font names are correct

The script will process files that contain the string: `Regular`, `Italic`, `Bold` and `BoldItalic`. This is the naming convention used on Kobo devices for proper compatibility with both the `epub` and `kepub` renderer. You must name your fonts correctly.

### Running with default preset

You can then run:

```bash
python3 kobofix.py --preset kf ./src/*.ttf
```

If no preset or flags are provided, the script will prompt you to choose a preset.

### Understanding the recommended preset

The recommended preset will prepare your fonts for use on a Kobo device. It's the primary reason why you'd want to run this script. It runs the following operations, in order:

1. **Validate all filenames.** If there are any invalid filenames, you will be prompted and can continue with all valid filenames, but it is recommended that you fix the invalid files.
2. **Remove any WWS name metadata from the font.** This is done because the font is renamed afterwards.
3. **Modify the internal name of the font.** The `KF` prefix is applied. Known prefixes (such as `NV` and `KF`) are automatically stripped before applying the new prefix. A custom name can also be specified with `--name`.
4. **PANOSE metadata is checked and fixed.** Sometimes, the PANOSE information does not match the font style. This is often an oversight but it causes issues on Kobo devices, so this fixes that.
5. **Font weight metadata is updated.** There's other metadata that is part of the font that reflects the weight of the font. In case this information needs to be modified, it is adjusted.
6. **Kern pairs from the GPOS table are copied to the legacy `kern` table.** This only applies to fonts that have a GPOS table, which is used for kerning in modern fonts. When there are more pairs than the format 0 limit (10,920), pairs are prioritized by Unicode range so that common Latin kerning is preserved.
7. **Outlines are simplified.** Overlapping contours are merged, degenerate (zero-area) contours are removed, and composite glyphs are flattened to simple outlines. This improves rendering consistency on e-ink displays. Can be disabled with `--outline skip`.
8. **TrueType hinting is removed and replaced with a no-op instruction.** A font with no per-glyph instructions falls into iType's *automatic* grid-fitting, which snaps each glyph's top to a whole pixel row depending on its sub-pixel position, so the same letter renders at slightly different heights from one place to the next — a visible vertical "wobble". iType routes a glyph that carries *any* instructions through its interpreter instead, bypassing the auto-grid-fit. So the KF preset removes global hinting tables and replaces every outline glyph's bytecode with a single one-byte no-op program (`SVTCA[Y]`, opcode `0x00`) that moves no points: the outline is emitted byte-for-byte unchanged, but iType now renders the raw scaled outline with no wobble.
9. **The final written font is validated with `ots-sanitize`.** If validation fails, that font is treated as a processing failure and the overall command exits non-zero.

You can also use a different preset. For example, the NV preset applies 20% line spacing, skips kerning, and leaves outlines untouched. See [Customization](#customization) and [Presets](#presets) for details.

## The Kobo "wobble" and how it's fixed

Some fonts render with a subtle vertical instability on Kobo e-readers: the *same* letter appears at slightly different heights in different places (for example, an `a` rasterized 26 px tall in one spot and 27 px in another, at the same size). The text looks faintly uneven, as if it were trembling. The same fonts render fine on desktop and on other e-readers — the defect is specific to Kobo's rendering path. Adding a no-op program actually forces `FT_LOAD_NO_HINTING`, which results in correct font rendering. You can learn more about this finding in the linked repository in the note below.

> [!NOTE]
> A device-level alternative exists: a Kobo mod named [NickelHintFix](https://github.com/nicoverbruggen/NickelHintFix) can hook `FT_Load_Glyph` and force `FT_LOAD_NO_HINTING`, fixing every font at once without touching the files. The no-op approach here is the font-level equivalent and it works on an unmodified Kobo and leaves the outlines untouched.

## Customization

You can customize what the script does. For more information, consult:

```bash
./kobofix.py -h
```

For debugging purposes, you can run the script with the `--verbose` flag.

## Presets

The script includes presets for common workflows. If no preset or flags are provided, you will be prompted to choose one.

### KF preset (for Kobo devices)

Prepares KF fonts from NV fonts for use on Kobo devices. This preset applies the KF prefix while stripping other common prefixes, adds a legacy kern table, simplifies outlines, flattens composite glyphs, removes TrueType hinting, and gives every outline glyph the same no-op instruction to suppress iType's wobble (see [The Kobo "wobble" and how it's fixed](#the-kobo-wobble-and-how-its-fixed)). No line spacing changes are made with this preset.

```bash
./kobofix.py --preset kf *.ttf
```

### NV preset (simpler use case)

This prepares fonts for [this](https://github.com/nicoverbruggen/ebook-fonts) repository. This preset applies the NV prefix and a 20% line spacing (generally tight). This does not modify kerning or simplify outlines, and the original fonts are mostly kept as-is. Useful if you want to rename a font and generate a variant with a different line height (more tight or relaxed spacing).

```bash
./kobofix.py --preset nv *.ttf
```

You can override individual settings, for example to use relaxed spacing:

```bash
./kobofix.py --preset nv --line-percent 50 *.ttf
```

### Custom processing

You can also specify all flags manually. For example, you can run:

```bash
./kobofix.py --prefix FNT --name="Fonty" --line-percent 33 --kern add-legacy-kern --outline apply *.ttf
```

## Testing

The repository includes a `unittest` suite that covers targeted font-table logic, end-to-end processing against real Readerly fonts, and the validator's OTS resolution logic.

Run the full suite with:

```bash
python3 -m unittest discover -s tests -v
```

On first run, the integration tests download the latest `Readerly.zip` release into `./tests/fixtures` and reuse those extracted fonts on later runs.

You can also validate generated fonts directly with:

```bash
python3 validate.py ./path/to/fonts/*.ttf
```

## License

This project is [MIT](/LICENSE) licensed.
