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
- Skip the `kern` step
- Use a custom name for a font
- Use a custom name for the prefix
- Remove the `GPOS` table entirely
- Adjust the percentage of the `font-line` setting
- Skip running `font-line` altogether

For debugging purposes, you can run the script with the `--verbose` flag.

## Examples

### Generating KF fonts

This applies the KF prefix, applies 20 percent line spacing and adds a Kobo `kern` table. Ideal if you have an existing TrueType font and you want it on your Kobo device.

The `--name` parameter is used to change the name of the font family.

```bash
./kobofix.py --prefix KF --name="Fonty" --line-percent 20 --remove-hints *.ttf
```

To process fonts from my [ebook-fonts](https://github.com/nicoverbruggen/ebook-fonts) collection which are prefixed with "NV", you can replace the prefix and make adjustments in bulk. 

To process all fonts with the "Kobo Fix" preset, simply run:

```bash
./kobofix.py --prefix KF --remove-prefix="NV" --line-percent 0 *.ttf
```

(In this case, we'll set --line-percent to 0 so the line height changes aren't made, because the fonts in the NV Collection should already have those changes applied.)

The expected output is then:

```
nico@m1ni kobo-font-fix % ./kobofix.py --prefix KF --remove-prefix NV *.ttf --line-percent 0

Processing: NV-Elstob-Bold.ttf
  --remove-prefix enabled: using 'Elstob' as the new family name.
  Renaming the font to: KF Elstob Bold
  PANOSE corrected: bWeight 8->8, bLetterForm 2->2
  Kerning: extracted 342467 pairs; wrote 342467 to legacy 'kern' table.
  Saved: KF_Elstob-Bold.ttf
  Skipping line adjustment step.

Processing: NV-Elstob-BoldItalic.ttf
  --remove-prefix enabled: using 'Elstob' as the new family name.
  Renaming the font to: KF Elstob Bold Italic
  PANOSE corrected: bWeight 8->8, bLetterForm 3->3
  Kerning: extracted 300746 pairs; wrote 300746 to legacy 'kern' table.
  Saved: KF_Elstob-BoldItalic.ttf
  Skipping line adjustment step.

Processing: NV-Elstob-Italic.ttf
  --remove-prefix enabled: using 'Elstob' as the new family name.
  Renaming the font to: KF Elstob Italic
  PANOSE corrected: bWeight 5->5, bLetterForm 3->3
  Kerning: extracted 286857 pairs; wrote 286856 to legacy 'kern' table.
  Saved: KF_Elstob-Italic.ttf
  Skipping line adjustment step.

Processing: NV-Elstob-Regular.ttf
  --remove-prefix enabled: using 'Elstob' as the new family name.
  Renaming the font to: KF Elstob
  PANOSE corrected: bWeight 5->5, bLetterForm 2->2
  Kerning: extracted 313998 pairs; wrote 313998 to legacy 'kern' table.
  Saved: KF_Elstob-Regular.ttf
  Skipping line adjustment step.

==================================================
Processed 4/4 fonts successfully.
```

### Generating NV fonts

Tight spacing, with a custom font family name:

```bash
./kobofix.py --prefix NV --name="Fonty" --line-percent 20 --skip-kobo-kern *.ttf
```

Relaxed spacing, with a custom font family name:

```bash
./kobofix.py --prefix NV --name="Fonty" --line-percent 50 --skip-kobo-kern *.ttf
```

You can play around with `--line-percent` to see what works for you.