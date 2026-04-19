# Kobo Font Fix

## Overview

**`kobofix.py` is a Python script designed to process and adjust TTF fonts for Kobo e-readers for a better reading experience with the default `kepub` renderer.**

It generates a renamed font, fixes PANOSE information based on the filename, adjusts the baseline with the `font-line` utility, simplifies outlines with `skia-pathops`, optionally controls hinting via `ttfautohint`, adds a legacy `kern` table which allows the `kepub` engine for improved rendering of kerned pairs, and validates finished output with `ots-sanitize` when that tool is already available.

You can use this to modify or fix your own, legally acquired fonts (assuming you are permitted to do so).

## License

Licensed under the [MIT License](/LICENSE).

## Requirements

Python 3, FontTools, `font-line`, and `skia-pathops`.

You can install them like so:

```bash
pip3 install fonttools font-line skia-pathops
```

If you want to use the `--hint additive` or `--hint overwrite` options, you also need `ttfautohint`:

```bash
brew install ttfautohint  # macOS
```

For standalone font validation, `validate.py` uses a system `ots-sanitize` binary when one is available. If it is not installed, the script downloads the latest compatible OTS release on first run and caches it under `./.tools`.

When `kobofix.py` finishes writing a processed font, it also runs `ots-sanitize` automatically if a system or cached binary is already available. If not, processing continues and the validation step is skipped with this warning: `WARNING: skipped ots-sanitize step (missing)`.

On macOS, if you're using the built-in version of Python (via Xcode), you may need to first add a folder to your `PATH` to make `font-line` available, like:

```bash
echo 'export PATH="$HOME/Library/Python/3.9/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

## Usage

Open a terminal and navigate to the directory containing your font files. Make sure your font files are named correctly. The script will process files that contain the string:

- `Regular`
- `Italic`
- `Bold`
- `BoldItalic`

This is the naming convention used on Kobo devices for proper compatibility with both the `epub` and `kepub` renderer.

You can then run:

```bash
python3 kobofix.py --preset kf ./src/*.ttf
```

If no preset or flags are provided, the script will prompt you to choose a preset. See the [Presets](#presets) section below for details.

With the Kobo Fix (KF) preset, the script will:

1. **Validate all filenames.** If there are any invalid filenames, you will be prompted and can continue with all valid filenames, but it is recommended that you fix the invalid files.
2. **Remove any WWS name metadata from the font.** This is done because the font is renamed afterwards.
3. **Modify the internal name of the font.** The `KF` prefix is applied. Known prefixes (such as `NV` and `KF`) are automatically stripped before applying the new prefix. A custom name can also be specified with `--name`.
4. **PANOSE metadata is checked and fixed.** Sometimes, the PANOSE information does not match the font style. This is often an oversight but it causes issues on Kobo devices, so this fixes that.
5. **Font weight metadata is updated.** There's other metadata that is part of the font that reflects the weight of the font. In case this information needs to be modified, it is adjusted.
6. **Kern pairs from the GPOS table are copied to the legacy `kern` table.** This only applies to fonts that have a GPOS table, which is used for kerning in modern fonts. When there are more pairs than the format 0 limit (10,920), pairs are prioritized by Unicode range so that common Latin kerning is preserved.
7. **Outlines are simplified.** Overlapping contours are merged and degenerate (zero-area) contours are removed. This improves rendering consistency on e-ink displays. Can be disabled with `--outline skip`.
8. **The final written font is validated with `ots-sanitize` when available.** If validation fails, that font is treated as a processing failure and the overall command exits non-zero. If `ots-sanitize` is not present, the validation step is skipped with a warning instead of downloading anything automatically.

Other presets and flags can change this behavior. For example, the NV preset applies 20% line spacing and skips kerning, and the `--hint` flag can be used to control hinting. 

See [Customization](#customization) and [Presets](#presets) for details.

The modified fonts are saved in the directory where the original fonts are located.

If `ots-sanitize` reports warnings but exits successfully, processing still succeeds and those warnings are shown in the output. A non-zero `ots-sanitize` exit code causes that font to fail validation.

## Customization

You can customize what the script does. For more information, consult:

```bash
./kobofix.py -h
```

Given the right arguments, you can:
- Control kerning behavior (`--kern`): add a legacy kern table, remove GPOS after extraction, or skip entirely (default: skip)
- Control hinting (`--hint`): strip hints, apply ttfautohint to unhinted fonts (`additive`), apply ttfautohint to all fonts (`overwrite`), or skip (default: skip)
- Control outline simplification (`--outline`): apply overlap removal and degenerate contour cleanup, or skip entirely (default: apply)
- Use a custom family name for a font (`--name`)
- Use a custom prefix (`--prefix`)
- Remove an existing prefix before applying the new one (`--remove-prefix`)
- Adjust the percentage of the `font-line` setting (`--line-percent`)
- Skip running `font-line` altogether (set `--line-percent 0`)
- Preview changes without modifying any files (`--dry-run`)

For debugging purposes, you can run the script with the `--verbose` flag.

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

## Presets

The script includes presets for common workflows. If no preset or flags are provided, you will be prompted to choose one.

### NV preset

Prepares fonts for the [ebook-fonts](https://github.com/nicoverbruggen/ebook-fonts) repository. Applies the NV prefix and 20% line spacing. Does not modify kerning, hinting, or outlines.

```bash
./kobofix.py --preset nv *.ttf
```

You can override individual settings, for example to use relaxed spacing:

```bash
./kobofix.py --preset nv --line-percent 50 *.ttf
```

### KF preset

Prepares KF fonts from NV fonts for use on Kobo devices. Applies the KF prefix, automatically strips known prefixes (NV, KF), adds a legacy kern table, and simplifies outlines. No line spacing changes are made (since NV fonts already have those applied).

```bash
./kobofix.py --preset kf *.ttf
```

### Custom processing

You can also specify all flags manually:

```bash
./kobofix.py --prefix KF --name="Fonty" --line-percent 20 --kern add-legacy-kern --outline apply *.ttf
```
