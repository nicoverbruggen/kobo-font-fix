from __future__ import annotations

import types
import unittest
import inspect
import tempfile
from pathlib import Path
from unittest import mock

from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables import ttProgram
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.ttGlyphPen import TTGlyphPen

from kobofix import FontMetadata, FontProcessor


class KobofixUnitTests(unittest.TestCase):
    def test_set_name_records_uses_utf16_for_unicode_platform(self) -> None:
        font = TTFont()
        name_table = newTable("name")
        name_table.names = []
        name_table.setName("Original", 1, 0, 3, 0x409)
        name_table.setName("Original", 1, 3, 1, 0x409)
        font["name"] = name_table

        FontProcessor._set_name_records(font, 1, "KF Readerly")

        unicode_record = font["name"].getName(1, 0, 3, 0x409)
        windows_record = font["name"].getName(1, 3, 1, 0x409)

        self.assertEqual(unicode_record.toUnicode(), "KF Readerly")
        self.assertEqual(windows_record.toUnicode(), "KF Readerly")
        self.assertEqual(unicode_record.string, "KF Readerly".encode("utf-16-be"))

    def test_extract_format2_pairs_includes_default_right_class(self) -> None:
        font = TTFont()
        font.setGlyphOrder([".notdef", "A", "B", "C"])

        processor = FontProcessor(prefix="KF", line_percent=0)
        subtable = types.SimpleNamespace(
            Coverage=types.SimpleNamespace(glyphs=["A"]),
            ClassDef1=types.SimpleNamespace(classDefs={}),
            ClassDef2=types.SimpleNamespace(classDefs={"B": 1}),
            Class1Record=[
                types.SimpleNamespace(
                    Class2Record=[
                        types.SimpleNamespace(
                            Value1=types.SimpleNamespace(XAdvance=-80),
                            Value2=None,
                        ),
                        types.SimpleNamespace(
                            Value1=types.SimpleNamespace(XAdvance=-40),
                            Value2=None,
                        ),
                    ]
                )
            ],
        )

        params = tuple(inspect.signature(processor._extract_format2_pairs).parameters)
        if len(params) == 2:
            pairs = processor._extract_format2_pairs(font, subtable)
        else:
            pairs = processor._extract_format2_pairs(subtable)

        self.assertEqual(pairs[("A", "B")], -40)
        self.assertEqual(pairs[("A", "C")], -80)

    def test_rename_font_updates_name_id_18_with_full_name(self) -> None:
        font = TTFont()
        name_table = newTable("name")
        name_table.names = []
        for platform_id, plat_enc_id, lang_id in ((1, 0, 0), (3, 1, 0x409)):
            name_table.setName("Readerly", 1, platform_id, plat_enc_id, lang_id)
            name_table.setName("Italic", 2, platform_id, plat_enc_id, lang_id)
            name_table.setName("Readerly Italic", 4, platform_id, plat_enc_id, lang_id)
            name_table.setName("Version 1.000", 3, platform_id, plat_enc_id, lang_id)
            name_table.setName("Readerly Ital", 18, platform_id, plat_enc_id, lang_id)
        font["name"] = name_table

        processor = FontProcessor(prefix="KF", line_percent=0)
        metadata = FontMetadata(
            family_name="Readerly",
            style_name="Italic",
            full_name="Readerly Italic",
            ps_name="KF_Readerly-Italic",
        )

        processor.rename_font(font, metadata)

        self.assertEqual(font["name"].getBestFullName(), "KF Readerly Italic")
        self.assertEqual(font["name"].getName(18, 1, 0, 0).toUnicode(), "KF Readerly Italic")

    def test_analyze_changes_handles_fonts_without_name_table(self) -> None:
        font = TTFont()
        processor = FontProcessor(prefix="KF", line_percent=0)
        metadata = FontMetadata(
            family_name="Readerly",
            style_name="Regular",
            full_name="Readerly",
            ps_name="KF_Readerly",
        )

        changes = processor._analyze_changes(
            font,
            "/tmp/Readerly-Regular.ttf",
            kern_mode="skip",
            metadata=metadata,
        )

        self.assertIn("Rename font to 'KF Readerly'", changes)

    def test_check_and_fix_panose_updates_expected_fields(self) -> None:
        font = TTFont()
        font["OS/2"] = types.SimpleNamespace(
            panose=types.SimpleNamespace(
                bFamilyType=2,
                bWeight=0,
                bLetterForm=0,
            )
        )

        processor = FontProcessor(prefix="KF", line_percent=0)
        processor.check_and_fix_panose(font, "/tmp/Readerly-BoldItalic.ttf")

        self.assertEqual(font["OS/2"].panose.bWeight, 8)
        self.assertEqual(font["OS/2"].panose.bLetterForm, 3)

    def test_update_weight_metadata_updates_os2_weight_class(self) -> None:
        font = TTFont()
        font["OS/2"] = types.SimpleNamespace(usWeightClass=400)

        processor = FontProcessor(prefix="KF", line_percent=0)
        processor.update_weight_metadata(font, "/tmp/Readerly-Bold.ttf")

        self.assertEqual(font["OS/2"].usWeightClass, 700)

    def test_resolve_family_name_strips_known_prefixes(self) -> None:
        font = TTFont()
        name_table = newTable("name")
        name_table.names = []
        name_table.setName("NV Readerly", 1, 3, 1, 0x409)
        font["name"] = name_table

        processor = FontProcessor(prefix="KF", line_percent=0)
        resolved = processor._resolve_family_name(font, new_name=None, remove_prefix=None)

        self.assertEqual(resolved, "Readerly")

    def test_glyph_priority_prefers_ascii_letters_digits_and_punctuation(self) -> None:
        cmap_reverse = {
            "A": ord("A"),
            "nine": ord("9"),
            "exclam": ord("!"),
            "space": ord(" "),
            "Aacute": 0x00C1,
            "Ccaron": 0x010C,
        }

        self.assertLess(
            FontProcessor._glyph_priority("A", cmap_reverse),
            FontProcessor._glyph_priority("space", cmap_reverse),
        )
        self.assertLess(
            FontProcessor._glyph_priority("nine", cmap_reverse),
            FontProcessor._glyph_priority("Aacute", cmap_reverse),
        )
        self.assertLess(
            FontProcessor._glyph_priority("exclam", cmap_reverse),
            FontProcessor._glyph_priority("Ccaron", cmap_reverse),
        )

    def test_glyph_priority_prefers_typographic_punctuation_over_extended_latin(self) -> None:
        cmap_reverse = {
            "quotedblleft": 0x201C,
            "quotedblright": 0x201D,
            "emdash": 0x2014,
            "ellipsis": 0x2026,
            "Aacute": 0x00C1,
            "Ccaron": 0x010C,
        }

        self.assertLess(
            FontProcessor._glyph_priority("quotedblleft", cmap_reverse),
            FontProcessor._glyph_priority("Aacute", cmap_reverse),
        )
        self.assertLess(
            FontProcessor._glyph_priority("emdash", cmap_reverse),
            FontProcessor._glyph_priority("Ccaron", cmap_reverse),
        )
        self.assertEqual(
            FontProcessor._glyph_priority("quotedblright", cmap_reverse),
            FontProcessor._glyph_priority("ellipsis", cmap_reverse),
        )

    def test_kf_noop_hinting_replaces_existing_hints_and_tables(self) -> None:
        pen = TTGlyphPen(None)
        pen.moveTo((0, 0))
        pen.lineTo((100, 0))
        pen.lineTo((100, 100))
        pen.lineTo((0, 100))
        pen.closePath()
        glyph = pen.glyph()
        original_program = ttProgram.Program()
        original_program.fromBytecode(b"\xb0\x01")
        glyph.program = original_program

        glyphs = {
            ".notdef": TTGlyphPen(None).glyph(),
            "A": glyph,
        }

        builder = FontBuilder(1000, isTTF=True)
        builder.setupGlyphOrder(list(glyphs))
        builder.setupGlyf(glyphs)
        builder.setupHorizontalMetrics({
            ".notdef": (500, 0),
            "A": (500, 0),
        })
        builder.setupHorizontalHeader(ascent=800, descent=-200)
        builder.setupMaxp()
        font = builder.font

        for tag in ("fpgm", "prep"):
            table = newTable(tag)
            table.program = ttProgram.Program()
            table.program.fromBytecode(b"\xb0\x00")
            font[tag] = table
        font["cvt "] = newTable("cvt ")
        font["cvt "].values = [0]

        removed = FontProcessor._strip_hinting_tables(font)
        changed = FontProcessor._add_noop_hints(font)

        self.assertEqual(removed, 3)
        self.assertEqual(changed, 1)
        self.assertNotIn("fpgm", font)
        self.assertNotIn("prep", font)
        self.assertNotIn("cvt ", font)
        self.assertEqual(font["glyf"]["A"].program.getBytecode(), FontProcessor._NOOP_BYTECODE)
        self.assertFalse(FontProcessor._font_has_meaningful_hints(font))
        self.assertFalse(FontProcessor._font_needs_noop_hints(font))
        self.assertEqual(font["maxp"].maxSizeOfInstructions, 1)

    def test_validate_output_font_fails_when_ots_is_missing(self) -> None:
        with self.assertLogs("kobofix", level="ERROR") as captured:
            with mock.patch.object(FontProcessor, "_find_available_ots", return_value=None):
                ok = FontProcessor._validate_output_font("/tmp/KF_Readerly-Regular.ttf")

        self.assertFalse(ok)
        self.assertIn("ots-sanitize is required", captured.output[0])

    def test_validate_output_font_uses_available_ots_binary(self) -> None:
        with mock.patch.object(FontProcessor, "_find_available_ots", return_value=Path("/usr/bin/ots-sanitize")):
            with mock.patch.object(FontProcessor, "_validate_font", return_value=(True, "File sanitized successfully!")) as run_validate:
                ok = FontProcessor._validate_output_font("/tmp/KF_Readerly-Regular.ttf")

        self.assertTrue(ok)
        run_validate.assert_called_once_with(
            Path("/usr/bin/ots-sanitize"),
            Path("/tmp/KF_Readerly-Regular.ttf"),
        )

    def test_flatten_composites_converts_components_to_simple_outlines(self) -> None:
        def rect_glyph(x_min: int, y_min: int, x_max: int, y_max: int):
            pen = TTGlyphPen(None)
            pen.moveTo((x_min, y_min))
            pen.lineTo((x_max, y_min))
            pen.lineTo((x_max, y_max))
            pen.lineTo((x_min, y_max))
            pen.closePath()
            return pen.glyph()

        component_pen = TTGlyphPen({"base": None, "mark": None})
        component_pen.addComponent("base", (1, 0, 0, 1, 0, 0))
        component_pen.addComponent("mark", (1, 0, 0, 1, 10, 20))

        glyphs = {
            ".notdef": TTGlyphPen(None).glyph(),
            "base": rect_glyph(0, 0, 100, 100),
            "mark": rect_glyph(0, 0, 20, 20),
            "base.mark": component_pen.glyph(),
        }

        builder = FontBuilder(1000, isTTF=True)
        builder.setupGlyphOrder(list(glyphs))
        builder.setupGlyf(glyphs)
        builder.setupHorizontalMetrics({
            ".notdef": (500, 0),
            "base": (500, 0),
            "mark": (0, 0),
            "base.mark": (500, 0),
        })
        builder.setupHorizontalHeader(ascent=800, descent=-200)
        builder.setupMaxp()
        font = builder.font

        self.assertTrue(font["glyf"]["base.mark"].isComposite())

        flattened = FontProcessor.flatten_composites(font)

        self.assertEqual(flattened, 1)
        self.assertFalse(font["glyf"]["base.mark"].isComposite())
        self.assertEqual(font["glyf"]["base.mark"].numberOfContours, 2)
        self.assertEqual(
            (
                font["glyf"]["base.mark"].xMin,
                font["glyf"]["base.mark"].yMin,
                font["glyf"]["base.mark"].xMax,
                font["glyf"]["base.mark"].yMax,
            ),
            (0, 0, 100, 100),
        )

    @staticmethod
    def _build_font_with_space(space_width: int = 500, upm: int = 1000):
        glyphs = {
            ".notdef": TTGlyphPen(None).glyph(),
            "space": TTGlyphPen(None).glyph(),
            "period": TTGlyphPen(None).glyph(),
            "zero": TTGlyphPen(None).glyph(),
        }
        builder = FontBuilder(upm, isTTF=True)
        builder.setupGlyphOrder(list(glyphs))
        builder.setupGlyf(glyphs)
        builder.setupHorizontalMetrics({
            ".notdef": (500, 0),
            "space": (space_width, 0),
            "period": (200, 0),
            "zero": (600, 0),
        })
        builder.setupHorizontalHeader(ascent=800, descent=-200)
        builder.setupCharacterMap({0x0020: "space", 0x002E: "period", 0x0030: "zero"})
        builder.setupMaxp()
        builder.setupNameTable({})
        builder.setupOS2()
        builder.setupPost()
        return builder.font

    def test_add_missing_spaces_adds_all_when_absent(self) -> None:
        from kobofix import SPACE_GLYPHS

        font = self._build_font_with_space(space_width=500)

        added = FontProcessor.add_missing_spaces(font)

        self.assertEqual(len(added), len(SPACE_GLYPHS))
        cmap = font.getBestCmap()
        for codepoint, _name, _spec in SPACE_GLYPHS:
            self.assertIn(codepoint, cmap)
            glyph_name = cmap[codepoint]
            self.assertEqual(font["glyf"][glyph_name].numberOfContours, 0)
        self.assertEqual(font["maxp"].numGlyphs, len(font.getGlyphOrder()))

    def test_add_missing_spaces_resolves_widths_per_spec(self) -> None:
        font = self._build_font_with_space(space_width=500, upm=1000)

        widths = dict(FontProcessor.add_missing_spaces(font))

        self.assertEqual(widths[0x2009], 250)  # thin: half of space (500)
        self.assertEqual(widths[0x202F], 250)  # narrow no-break: same as thin
        self.assertEqual(widths[0x2003], 1000) # em space: 1 em
        self.assertEqual(widths[0x2002], 500)  # en space: 1/2 em
        self.assertEqual(widths[0x2006], round(1000 / 6))  # six-per-em
        self.assertEqual(widths[0x2007], 600)  # figure space: digit width
        self.assertEqual(widths[0x2008], 200)  # punctuation space: period width
        self.assertEqual(widths[0x200B], 0)    # zero width space

    def test_add_missing_spaces_is_noop_when_already_present(self) -> None:
        font = self._build_font_with_space()
        FontProcessor.add_missing_spaces(font)
        glyph_count = len(font.getGlyphOrder())

        # Second call must not add duplicates or change anything.
        result = FontProcessor.add_missing_spaces(font)

        self.assertEqual(result, [])
        self.assertEqual(len(font.getGlyphOrder()), glyph_count)

    def test_add_missing_spaces_space_width_falls_back_without_space(self) -> None:
        font = self._build_font_with_space(upm=2000)
        # Drop the space mapping so "space"-derived widths fall back to 1/5 em.
        for table in font["cmap"].tables:
            table.cmap.pop(0x0020, None)

        widths = dict(FontProcessor.add_missing_spaces(font))

        self.assertEqual(widths[0x2009], 400)  # 2000 / 5

    @staticmethod
    def _build_font_with_hyphen_and_emdash(upm: int = 1000):
        def rect(x0, y0, x1, y1):
            pen = TTGlyphPen(None)
            pen.moveTo((x0, y0))
            pen.lineTo((x1, y0))
            pen.lineTo((x1, y1))
            pen.lineTo((x0, y1))
            pen.closePath()
            return pen.glyph()

        glyphs = {
            ".notdef": TTGlyphPen(None).glyph(),
            "hyphen": rect(50, 200, 300, 280),
            "emdash": rect(0, 200, 900, 280),
        }
        builder = FontBuilder(upm, isTTF=True)
        builder.setupGlyphOrder(list(glyphs))
        builder.setupGlyf(glyphs)
        builder.setupHorizontalMetrics({
            ".notdef": (500, 0),
            "hyphen": (350, 50),
            "emdash": (1000, 0),
        })
        builder.setupHorizontalHeader(ascent=800, descent=-200)
        builder.setupCharacterMap({0x002D: "hyphen", 0x2014: "emdash"})
        builder.setupMaxp()
        builder.setupNameTable({})
        builder.setupOS2()
        builder.setupPost()
        return builder.font

    def test_add_missing_clones_duplicates_hyphen_shape_and_width(self) -> None:
        font = self._build_font_with_hyphen_and_emdash()

        added = dict(FontProcessor.add_missing_clones(font))

        # Soft hyphen, hyphen and non-breaking hyphen all come from the hyphen;
        # the horizontal bar comes from the em dash.
        self.assertEqual(added[0x00AD], "hyphen")
        self.assertEqual(added[0x2011], "hyphen")
        self.assertEqual(added[0x2015], "emdash")

        cmap = font.getBestCmap()
        nbhyphen = cmap[0x2011]
        # Same advance width and left side bearing as the source hyphen.
        self.assertEqual(font["hmtx"][nbhyphen], font["hmtx"]["hyphen"])
        # A real (non-empty) outline, matching the hyphen's contour count.
        self.assertEqual(
            font["glyf"][nbhyphen].numberOfContours,
            font["glyf"]["hyphen"].numberOfContours,
        )
        self.assertGreater(font["glyf"][nbhyphen].numberOfContours, 0)
        # Independent copy: mutating the clone must not touch the source.
        self.assertIsNot(font["glyf"][nbhyphen], font["glyf"]["hyphen"])
        self.assertEqual(font["maxp"].numGlyphs, len(font.getGlyphOrder()))

    def test_add_missing_clones_is_noop_when_present(self) -> None:
        font = self._build_font_with_hyphen_and_emdash()
        FontProcessor.add_missing_clones(font)
        count = len(font.getGlyphOrder())

        result = FontProcessor.add_missing_clones(font)

        self.assertEqual(result, [])
        self.assertEqual(len(font.getGlyphOrder()), count)

    def test_add_missing_figure_dash_uses_digit_width_and_centers(self) -> None:
        font = self._build_font_with_hyphen_and_emdash()
        # Give the font a '0' so the figure width can be measured (width 700,
        # deliberately different from the em dash's advance of 1000).
        zero = TTGlyphPen(None)
        zero.moveTo((100, 0)); zero.lineTo((600, 0))
        zero.lineTo((600, 700)); zero.lineTo((100, 700)); zero.closePath()
        order = font.getGlyphOrder() + ["zero"]
        font["glyf"]["zero"] = zero.glyph()
        font.setGlyphOrder(order)
        font["hmtx"]["zero"] = (700, 100)
        for table in font["cmap"].tables:
            if table.isUnicode():
                table.cmap[0x0030] = "zero"

        width = FontProcessor.add_missing_figure_dash(font)

        self.assertEqual(width, 700)  # the '0' advance width
        cmap = font.getBestCmap()
        name = cmap[0x2012]
        self.assertEqual(font["hmtx"][name][0], 700)
        glyph = font["glyf"][name]
        self.assertGreater(glyph.numberOfContours, 0)  # real dash outline
        # Ink is centred: left and right side bearings are equal (±1 rounding).
        lsb = glyph.xMin
        rsb = 700 - glyph.xMax
        self.assertLessEqual(abs(lsb - rsb), 1)

    def test_add_missing_figure_dash_is_noop_when_present(self) -> None:
        font = self._build_font_with_hyphen_and_emdash()
        for table in font["cmap"].tables:
            if table.isUnicode():
                table.cmap[0x2012] = "emdash"

        self.assertIsNone(FontProcessor.add_missing_figure_dash(font))

    def test_add_missing_figure_dash_falls_back_to_hyphen(self) -> None:
        font = self._build_font_with_hyphen_and_emdash()
        # Remove the en dash and add a '0' so the hyphen becomes the only dash.
        for table in font["cmap"].tables:
            table.cmap.pop(0x2014, None)
            if table.isUnicode():
                table.cmap[0x0030] = "hyphen"  # any digit width source

        width = FontProcessor.add_missing_figure_dash(font)

        self.assertIsNotNone(width)
        cmap = font.getBestCmap()
        self.assertIn(0x2012, cmap)
        self.assertGreater(font["glyf"][cmap[0x2012]].numberOfContours, 0)

    def test_add_missing_figure_dash_returns_none_without_any_dash(self) -> None:
        font = self._build_font_with_hyphen_and_emdash()
        for table in font["cmap"].tables:
            table.cmap.pop(0x002D, None)
            table.cmap.pop(0x2014, None)

        self.assertIsNone(FontProcessor.add_missing_figure_dash(font))

    def test_digit_width_prefers_zero_then_falls_back(self) -> None:
        font = self._build_font_with_space()  # has 'zero' mapped at U+0030 (600)
        cmap = font.getBestCmap()
        self.assertEqual(FontProcessor._digit_width(font, cmap), 600)

        # With '0' absent, it falls back to the next available digit.
        for table in font["cmap"].tables:
            table.cmap.pop(0x0030, None)
            if table.isUnicode():
                table.cmap[0x0031] = "period"  # stand-in digit glyph (width 200)
        cmap = font.getBestCmap()
        self.assertEqual(FontProcessor._digit_width(font, cmap), 200)

    def test_added_outline_glyphs_get_noop_but_spaces_stay_empty(self) -> None:
        # The cloned hyphen and figure dash are real outlines added right before
        # KF no-op instrumentation, so they must receive the no-op program;
        # the empty space glyphs must stay contour-less and un-hinted.
        font = self._build_font_with_hyphen_and_emdash()
        # Add a space and a digit so widths resolve.
        for name, metrics in (("space", (500, 0)), ("zero", (600, 100))):
            font["glyf"][name] = TTGlyphPen(None).glyph()
            font.setGlyphOrder(font.getGlyphOrder() + [name])
            font["hmtx"][name] = metrics
        # give 'zero' a real contour so it's a valid digit glyph
        zero = TTGlyphPen(None)
        zero.moveTo((100, 0)); zero.lineTo((500, 0))
        zero.lineTo((500, 700)); zero.lineTo((100, 700)); zero.closePath()
        font["glyf"]["zero"] = zero.glyph()
        for table in font["cmap"].tables:
            if table.isUnicode():
                table.cmap[0x0020] = "space"
                table.cmap[0x0030] = "zero"

        FontProcessor.add_missing_spaces(font)
        FontProcessor.add_missing_clones(font)
        FontProcessor.add_missing_figure_dash(font)
        FontProcessor._add_noop_hints(font)

        cmap = font.getBestCmap()
        noop = FontProcessor._NOOP_BYTECODE
        # Outline glyphs we synthesised carry the no-op program.
        for cp in (0x2011, 0x2012, 0x2015):  # nb-hyphen, figure dash, h-bar
            glyph = font["glyf"][cmap[cp]]
            self.assertEqual(glyph.program.getBytecode(), noop)
        # Empty space glyphs stay contour-less and receive no program.
        for cp in (0x2009, 0x2003, 0x200B):  # thin, em space, zero-width space
            glyph = font["glyf"][cmap[cp]]
            self.assertEqual(glyph.numberOfContours, 0)
            self.assertFalse(hasattr(glyph, "program") and glyph.program)

    def test_add_missing_clones_skips_when_no_source_glyph(self) -> None:
        font = self._build_font_with_hyphen_and_emdash()
        # Remove every source mapping so nothing can be cloned.
        for table in font["cmap"].tables:
            table.cmap.pop(0x002D, None)
            table.cmap.pop(0x2014, None)

        result = FontProcessor.add_missing_clones(font)

        self.assertEqual(result, [])

    def test_process_otf_converts_to_ttf_and_removes_old_cff_names(self) -> None:
        glyph_order = [".notdef", "A"]

        empty_pen = T2CharStringPen(500, None)
        a_pen = T2CharStringPen(500, None)
        a_pen.moveTo((0, 0))
        a_pen.lineTo((400, 0))
        a_pen.lineTo((400, 600))
        a_pen.lineTo((0, 600))
        a_pen.closePath()

        charstrings = {
            ".notdef": empty_pen.getCharString(),
            "A": a_pen.getCharString(),
        }

        builder = FontBuilder(1000, isTTF=False)
        builder.setupGlyphOrder(glyph_order)
        builder.setupCharacterMap({ord("A"): "A"})
        builder.setupHorizontalMetrics({
            ".notdef": (500, 0),
            "A": (500, 0),
        })
        builder.setupHorizontalHeader(ascent=800, descent=-200)
        builder.setupNameTable({
            "familyName": "Old CFF Name",
            "styleName": "Regular",
            "uniqueFontIdentifier": "Old CFF Name Regular",
            "fullName": "Old CFF Name Regular",
            "psName": "OldCFFName-Regular",
            "version": "Version 1.000",
        })
        builder.setupOS2(
            sTypoAscender=800,
            sTypoDescender=-200,
            usWinAscent=800,
            usWinDescent=200,
        )
        builder.setupPost()
        builder.setupCFF(
            "OldCFFName-Regular",
            {
                "FullName": "Old CFF Name Regular",
                "FamilyName": "Old CFF Name",
                "Weight": "Regular",
            },
            charstrings,
            {},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "Converted-Regular.otf"
            builder.save(input_path)

            processor = FontProcessor(prefix="KF", line_percent=0)
            with mock.patch.object(FontProcessor, "_validate_output_font", return_value=True):
                ok = processor.process_font(
                    kern_mode="skip",
                    font_path=str(input_path),
                    new_name="Converted Serif",
                    outline_mode="skip",
                )

            self.assertTrue(ok)
            output_path = Path(tmpdir) / "KF_Converted_Serif-Regular.ttf"
            output_font = TTFont(output_path)

            self.assertEqual(output_font.sfntVersion, "\x00\x01\x00\x00")
            self.assertIn("glyf", output_font)
            self.assertNotIn("CFF ", output_font)
            self.assertEqual(output_font["name"].getBestFamilyName(), "KF Converted Serif")
            self.assertEqual(output_font["name"].getBestFullName(), "KF Converted Serif")
            self.assertEqual(output_font["name"].getName(6, 3, 1, 0x0409).toUnicode(), "KF_Converted-Serif")

            retained_names = [
                record.toUnicode()
                for record in output_font["name"].names
                if "Old CFF Name" in record.toUnicode() or "OldCFFName" in record.toUnicode()
            ]
            self.assertEqual(retained_names, [])


if __name__ == "__main__":
    unittest.main()
