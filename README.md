# Kobo Font Fix

## Overview

**`kobofix.py` is a Python script designed to process and adjust TTF fonts for Kobo e-readers for a better reading experience with the default `kepub` renderer.**

It generates a renamed font, fixes PANOSE information based on the filename, adjusts the baseline with the `font-line` utility, and adds a legacy `kern` table which allows the `kepub` engine for improved rendering of kerned pairs.

You can use this to modify or fix your own, legally acquired fonts (assuming you are permitted to do so).

## License

Licensed under the [MIT License](/LICENSE).

## Requirements

Python 3, FontTools, `font-line`.

You can install them like so:

```bash
pip3 install fonttools
pip3 install font-line
```

If you want to use the `--hint additive` or `--hint overwrite` options, you also need `ttfautohint`:

```bash
brew install ttfautohint  # macOS
```

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
python3 kobofix.py ./src/*.ttf
```

By default, the script will:

1. **Validate all filenames.** If there are any invalid filenames, you will be prompted and can continue with all valid filenames, but it is recommended that you fix the invalid files.
2. **Remove any WWS name metadata from the font.** This is done because the font is renamed afterwards.
3. **Modify the internal name of the font.** Unless a new name was specified, this is merely a prefix that is applied. (By default, this is `KF`.)
4. **PANOSE metadata is checked and fixed.** Sometimes, the PANOSE information does not match the font style. This is often an oversight but it causes issues on Kobo devices, so this fixes that.
5. **Font weight metadata is updated.** There's other metadata that is part of the font that reflects the weight of the font. In case this information needs to be modified, it is adjusted.
6. **Kern pairs from the GPOS table are copied to the legacy `kern` table.** This only applies to fonts that have a GPOS table, which is used for kerning in modern fonts.
7. **The `font-line` helper is used to apply a 20% line-height setting.** This generates a new file which is immediately renamed to the desired output format.

The modified fonts are saved in the directory where the original fonts are located.

## Customization

You can customize what the script does. For more information, consult:

```bash
./kobofix.py -h
```

Given the right arguments, you can:
- Control kerning behavior (`--kern`): add a legacy kern table (default), remove GPOS after extraction, or skip entirely
- Control hinting (`--hint`): strip hints, apply ttfautohint to unhinted fonts, apply ttfautohint to all fonts, or skip (default)
- Use a custom name for a font
- Use a custom name for the prefix
- Adjust the percentage of the `font-line` setting
- Skip running `font-line` altogether

For debugging purposes, you can run the script with the `--verbose` flag.

## Presets

The script includes presets for common workflows. If no preset or flags are provided, you will be prompted to choose one.

### NV preset

Prepares fonts for the [ebook-fonts](https://github.com/nicoverbruggen/ebook-fonts) repository. Applies the NV prefix and 20% line spacing. Does not modify kerning or hinting.

```bash
./kobofix.py --preset nv *.ttf
```

You can override individual settings, for example to use relaxed spacing:

```bash
./kobofix.py --preset nv --line-percent 50 *.ttf
```

### KF preset

Prepares KF fonts from NV fonts for use on Kobo devices. Applies the KF prefix, replaces the NV prefix, and adds a legacy kern table. No line spacing changes are made (since NV fonts already have those applied).

```bash
./kobofix.py --preset kf *.ttf
```

### Custom processing

You can also specify all flags manually:

```bash
./kobofix.py --prefix KF --name="Fonty" --line-percent 20 --kern add-legacy-kern *.ttf
```