"""TNEF-Leser (winmail.dat aus Outlook): Aufbau, Namen, Codepages, Eigenschaften, Mailtext (LZFu/MELA/RTF) und
Robustheit gegen kaputte Ströme. Nur erfundene Daten; die Ströme baut tests/conftest.py (tnef_bytes)."""
import random
import struct
import time

import pytest

from belegsuche import tnef

from .conftest import (MV, PS_PUBLIC_STRINGS, PT_BINARY, PT_LONG, PT_OBJECT, PT_STRING8, PT_UNICODE, html_rtf, lzfu,
                       mapi_props, mela, tnef_attachment, tnef_attr, tnef_bytes)

PDF = b"%PDF-1.4\n" + b"Rechnung RE-2024/0815 Gartenbau Eiche\n" * 20 + b"%%EOF\n"
JPEG = b"\xff\xd8\xff\xe0" + bytes(500)
PNG = b"\x89PNG\r\n\x1a\n" + bytes(300)
SIG_KEY = b"\x78\x9f\x3e\x22\x00\x00"                 # Signatur und Schlüssel


# --- Aufbau wie im Bestand ----------------------------------------------------------------------------------

def test_standard_stream_with_file_attachment_variant_a():
    """P1: neun Nachrichten-Attribute, attMsgProps mit benannten Eigenschaften, Empfängertabelle, ein PDF in
    Bauform A (Renddata, Data, Title, MetaFile, CreateDate, ModifyDate, Attachment), Codepage 28591."""
    data = tnef_bytes([tnef_attachment(PDF, "Rechnung Gartenbau 2024.pdf", metafile=True, dates=True)])
    msg = tnef.parse(data)
    assert msg.notes == [] and msg.signature_ok
    assert msg.oem_codepage == 28591 and msg.codepage == 28591
    assert msg.subject == "Rechnung Juli" and msg.message_class == "IPM.Note"
    [att] = msg.attachments
    assert att.data == PDF and att.name == "Rechnung Gartenbau 2024.pdf" and att.method == 1
    assert not att.inline and not att.embedded and not att.truncated


def test_variant_c_name_only_in_attach_filename_beats_title_with_question_marks():
    """P1: Bauform C – kein 0x3707/0x3001; der lange Name mit Umlaut steht nur in 0x3704, der Titel hat „?“."""
    msg = tnef.parse(tnef_bytes([tnef_attachment(JPEG, "Schadensfoto Kellerraum Bäder ünd Flur.jpg",
                                                 variant="C")]))
    [att] = msg.attachments
    assert att.title == "Schadensfoto Kellerraum B?der ?nd Flur.jpg"
    assert att.long_name is None and att.display_name is None
    assert att.name == "Schadensfoto Kellerraum Bäder ünd Flur.jpg"


def test_name_priority_long_display_short_title():
    a = tnef_attachment(PDF, "x.pdf", variant="C", title="TITEL.PDF", extra=[
        (PT_UNICODE, 0x3001, "Anzeige.pdf"), (PT_UNICODE, 0x3707, "Langer Name Mietvertrag.pdf")])
    assert tnef.parse(tnef_bytes([a])).attachments[0].name == "Langer Name Mietvertrag.pdf"
    a = tnef_attachment(PDF, "x.pdf", variant="C", title="TITEL.PDF", extra=[(PT_UNICODE, 0x3001, "Anzeige.pdf")])
    assert tnef.parse(tnef_bytes([a])).attachments[0].name == "Anzeige.pdf"
    a = tnef_attachment(PDF, "", variant="C", title="NUR~1.PDF")
    assert tnef.parse(tnef_bytes([a])).attachments[0].name == "NUR~1.PDF"


def test_inline_logos_variant_b_and_flags():
    """P1: Bauform B (MIME-Tag, Content-ID, Kennzeichen 4, versteckt) ist inline; Datei-Anhänge nicht."""
    atts = [tnef_attachment(PNG, f"image00{i}.png", variant="B", cid=f"image00{i}.png@01DA") for i in range(1, 8)]
    atts.append(tnef_attachment(PDF, "Angebot.pdf"))
    msg = tnef.parse(tnef_bytes(atts))
    assert [a.inline for a in msg.attachments] == [True] * 7 + [False]
    assert msg.attachments[0].mime == "image/png" and msg.attachments[0].content_id == "image001.png@01DA"
    assert msg.attachments[0].flags == 4 and msg.attachments[0].hidden
    hidden_only = tnef_attachment(JPEG, "foto.jpg", variant="C", extra=[(0x000B, 0x7FFE, True)])
    assert tnef.parse(tnef_bytes([hidden_only])).attachments[0].inline


def test_stream_without_attachments():
    msg = tnef.parse(tnef_bytes([]))
    assert msg.attachments == [] and msg.notes == [] and msg.n_attributes == 9


@pytest.mark.parametrize("cp, subject", [(65001, "Grüße"), (28591, "Grüße"), (28605, "Grüße €"), (20127, "Gruesse"),
                                         (1252, "Grüße"), (99999, "Grüße"), (0, "Grüße")])
def test_codepages_never_raise(cp, subject):
    """P1: 28591 kennt Python nicht als „cp28591“; unbekannte Codepages fallen auf Windows-1252 zurück."""
    codec = {65001: "utf-8", 28605: "iso8859-15", 20127: "ascii"}.get(cp, "latin-1")
    raw = subject.encode(codec)
    data = tnef_bytes([], codepage=cp)
    data += tnef_attr(1, 0x00018004, raw + b"\x00")              # zweites attSubject: zählt nicht
    msg = tnef.parse(data)
    assert msg.notes == []
    for name in (tnef.codec_name(cp), tnef.codec_name(None)):
        "äöü".encode(name, errors="replace")
    assert tnef.codec_name(28591) == "cp1252" and tnef.codec_name(65001) == "utf-8"
    assert tnef.codec_name(28605) == "iso8859-15" and tnef.codec_name(99999) == "cp1252"


def test_string8_names_use_the_codepage():
    a = tnef_attachment(PDF, "Kündigung.pdf", name8=True)
    assert tnef.parse(tnef_bytes([a], codepage=1252)).attachments[0].name == "Kündigung.pdf"


def test_named_properties_and_multivalues_are_skipped_correctly():
    """P1: benannte Eigenschaften (Nummer und Name, ungerade Zeichenzahl) und Mehrfachwerte vor den
    gesuchten Eigenschaften – die Liste wird bis zum Ende gelesen."""
    extra = [(PT_BINARY | MV, 0x8005, [b"a", b"bcdef", b""], (PS_PUBLIC_STRINGS, "Abcde")),
             (PT_LONG | MV, 0x8006, [1, 2, 3], (PS_PUBLIC_STRINGS, 0x1234)),
             (0x0002 | MV, 0x8007, [1, 2, 3], (PS_PUBLIC_STRINGS, "X")),
             (0x0048, 0x8008, bytes(16), (PS_PUBLIC_STRINGS, "Klasse")),
             (PT_UNICODE, 0x1000, "Text nach den benannten Eigenschaften Kellerschlüssel")]
    msg = tnef.parse(tnef_bytes([], extra_props=extra))
    assert msg.notes == []
    assert msg.body == "Text nach den benannten Eigenschaften Kellerschlüssel"
    props, note = tnef.parse_props(mapi_props([(0x0005, 0x0001, 0), (0x0014, 0x0002, 0), (PT_LONG, 0x0003, 7)]))
    assert note is None and [p[1] for p in props] == [1, 2, 3]


def test_data_in_props_instead_of_attach_data_and_attachment_without_renddata():
    """P2: PR_ATTACH_DATA_BIN statt attAttachData; ein Anhang ohne attAttachRendData am Anfang."""
    a1 = tnef_attachment(PDF, "a.pdf", data_in_props=True)
    a2 = tnef_attachment(JPEG, "b.jpg", rend=False)
    a3 = tnef_attachment(PNG, "c.png", rend=False)
    msg = tnef.parse(tnef_bytes([a1, a2, a3]))
    assert [(a.name, a.data) for a in msg.attachments] == [("a.pdf", PDF), ("b.jpg", JPEG), ("c.png", PNG)]


def test_embedded_message_ole_and_reference_attachment():
    """P2: eingebettete Nachricht (Methode 5, PT_OBJECT mit IID_IMessage und eigenem TNEF-Strom), OLE-Objekt
    (Methode 6, IID_IStorage) und ein Verweis-Anhang (Methode 2) ohne Daten."""
    inner = tnef_bytes([tnef_attachment(PDF, "innen.pdf")], subject="Weitergeleitet: Angebot")
    emb = tnef_attachment(None, "Weitergeleitet: Angebot", method=5, embedded=inner)
    ole = tnef_attachment(None, "Objekt", method=6, ole=b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(100))
    ref = tnef_attachment(None, "Verweis.pdf", method=2)
    msg = tnef.parse(tnef_bytes([emb, ole, ref]))
    e, o, r = msg.attachments
    assert e.embedded and e.data == inner and e.name == "Weitergeleitet: Angebot"
    assert tnef.parse(e.data).attachments[0].data == PDF
    assert o.ole and not o.embedded and o.data.startswith(b"\xd0\xcf\x11\xe0")
    assert r.data is None and r.method == 2


def test_message_classes_meeting_and_smime_are_read_like_others():
    msg = tnef.parse(tnef_bytes([tnef_attachment(b"0\x80" + bytes(50), "smime.p7m", variant="C")],
                                message_class="IPM.Note.SMIME"))
    assert msg.message_class == "IPM.Note.SMIME" and msg.attachments[0].name == "smime.p7m"
    msg = tnef.parse(tnef_bytes([], message_class="IPM.Schedule.Meeting.Request"))
    assert msg.message_class == "IPM.Schedule.Meeting.Request" and msg.notes == []


# --- kaputte Ströme: liefern, was lesbar ist, und einen Hinweis ------------------------------------------------

def test_wrong_checksum_keeps_reading_with_note():
    good = tnef_bytes([tnef_attachment(PDF, "a.pdf")])
    i = good.index(PDF) + len(PDF)                  # Prüfsumme direkt hinter den Daten
    bad = good[:i] + bytes([good[i] ^ 0xFF]) + good[i + 1:]
    msg = tnef.parse(bad)
    assert msg.attachments[0].data == PDF and msg.attachments[0].name == "a.pdf"
    assert msg.notes == ["Prüfsumme falsch (1 Attribut)"]


def test_truncated_stream_delivers_what_is_readable():
    good = tnef_bytes([tnef_attachment(PDF, "erst.pdf"), tnef_attachment(JPEG, "zweit.jpg")])
    cut = good[:good.index(JPEG) + 100]             # mitten in den Daten des zweiten Anhangs
    msg = tnef.parse(cut)
    a, b = msg.attachments
    assert a.data == PDF and a.name == "erst.pdf" and not a.truncated
    assert b.truncated and b.data == JPEG[:100] and "abgeschnitten" in msg.notes


@pytest.mark.parametrize("tail, note", [(b"\x01\x02\x03", "abgeschnitten (3 Bytes Rest)"),
                                        (b"\x07" + bytes(12), "unbekannte Ebene 7")])
def test_trailing_garbage_and_unknown_level(tail, note):
    msg = tnef.parse(tnef_bytes([tnef_attachment(PDF, "a.pdf")]) + tail)
    assert msg.attachments[0].data == PDF and any(note in n for n in msg.notes)


def test_missing_checksum_at_the_end():
    good = tnef_bytes([tnef_attachment(PDF, "a.pdf")])
    msg = tnef.parse(good[:-2])                      # letzte Prüfsumme fehlt
    assert msg.attachments[0].data == PDF and "abgeschnitten" in msg.notes


def test_huge_length_and_count_fields_do_not_allocate():
    """P2: Längen nahe 4 GB und 10⁶ Eigenschaften: kein Speicherüberlauf, schnell, Hinweis."""
    head = tnef_bytes([])
    huge_attr = struct.pack("<BII", 2, 0x0006800F, 0xFFFFFFF0) + b"%PDF" + bytes(50)
    t0 = time.time()
    msg = tnef.parse(head + huge_attr)
    assert "abgeschnitten" in msg.notes and msg.attachments[0].truncated
    props = struct.pack("<I", 1_000_000) + struct.pack("<HHI", PT_BINARY, 0x3701, 1) + struct.pack("<I", 0xFFFFFFF0)
    msg = tnef.parse(head + tnef_attr(2, 0x00069002, bytes(14)) + tnef_attr(2, 0x00069005, props))
    assert any("Eigenschaft" in n for n in msg.notes)
    mv = struct.pack("<I", 1) + struct.pack("<HHI", PT_UNICODE | MV, 0x3707, 0x7FFFFFFF)
    msg = tnef.parse(head + tnef_attr(2, 0x00069002, bytes(14)) + tnef_attr(2, 0x00069005, mv))
    assert any("abgeschnitten" in n for n in msg.notes)
    named = struct.pack("<I", 1) + struct.pack("<HH", PT_LONG, 0x8001) + bytes(16) + struct.pack("<II", 1, 0xFFFFFFF0)
    msg = tnef.parse(head + tnef_attr(1, 0x00069003, named))
    assert any("abgeschnitten" in n for n in msg.notes)
    many = struct.pack("<I", 1_000_000) + struct.pack("<HHI", PT_LONG, 0x0E21, 1) * 30_000
    msg = tnef.parse(head + tnef_attr(1, 0x00069003, many))
    assert any("zu viele Eigenschaften" in n for n in msg.notes)
    assert time.time() - t0 < 5


def test_too_many_attachments_are_capped(monkeypatch):
    monkeypatch.setattr(tnef, "MAX_ATTACHMENTS", 5)
    msg = tnef.parse(tnef_bytes([tnef_attachment(PNG, f"{i}.png", variant="B") for i in range(8)]))
    assert len(msg.attachments) == 5 and any("mehr als 5 Anhänge" in n for n in msg.notes)


def test_not_a_tnef_stream():
    for data in (b"", b"\x78\x9f", b"PK\x03\x04" + bytes(20)):
        msg = tnef.parse(data)
        assert not msg.signature_ok and msg.attachments == [] and msg.notes


def test_random_damage_never_raises():
    """Zufällig verstümmelte Ströme (Bytes ersetzt, abgeschnitten, Stücke eingefügt): nie eine Ausnahme."""
    good = tnef_bytes([tnef_attachment(PDF, "Rechnung Müller.pdf", metafile=True, dates=True),
                       tnef_attachment(PNG, "image001.png", variant="B"),
                       tnef_attachment(JPEG, "Foto Bäder.jpg", variant="C")], rtf=lzfu(html_rtf(["Hallo"])))
    rnd = random.Random(4711)
    t0 = time.time()
    for _ in range(400):
        b = bytearray(good)
        for _ in range(rnd.randint(1, 8)):
            op = rnd.random()
            i = rnd.randrange(len(b))
            if op < 0.5:
                b[i] = rnd.randrange(256)
            elif op < 0.7:
                b[i:i + 4] = struct.pack("<I", rnd.choice([0, 1, 0xFFFFFFFF, 0x7FFFFFFF, rnd.randrange(1 << 32)]))
            elif op < 0.85:
                del b[i:]
                if len(b) < 10:
                    b = bytearray(good[:10])
            else:
                b[i:i] = bytes(rnd.randrange(256) for _ in range(rnd.randint(1, 20)))
        msg = tnef.parse(bytes(b))
        msg.text()
        for a in msg.attachments:
            assert a.data is None or len(a.data) <= len(b)
    assert time.time() - t0 < 30


# --- Mailtext: PR_BODY, HTML, RTF (LZFu, MELA) -------------------------------------------------------------

def test_lzfu_test_vectors_from_the_specification():
    """MS-OXRTFCP 4.1/4.2: einfaches RTF und ein Verweis, der über die Schreibposition reicht."""
    v1 = bytes.fromhex("2d0000002b0000004c5a4675f1c5c7a703000a00726370673132354232"
                       "0af32068656c090020627705b06c647d0a800fa0")
    assert tnef.decompress_rtf(v1) == (b"{\\rtf1\\ansi\\ansicpg1252\\pard hello world}\r\n", None)
    v2 = bytes.fromhex("1a0000001c0000004c5a4675e2d44b51410004205758595a0d6e7d010eb0")
    assert tnef.decompress_rtf(v2) == (b"{\\rtf1 WXYZWXYZWXYZWXYZWXYZ}", None)
    assert tnef.rtf_to_text(tnef.decompress_rtf(v1)[0]) == ("text", "hello world")


def test_lzfu_wrong_crc_mela_and_unknown_compression():
    rtf = b"{\\rtf1\\ansi\\fromtext \\fbidis Zaehlerstand 4711\\par}"
    comp = bytearray(lzfu(rtf))
    comp[12] ^= 0xFF                                 # CRC falsch: Text trotzdem, mit Hinweis
    out, note = tnef.decompress_rtf(bytes(comp))
    assert out == rtf and note == "RTF-Mailtext: Prüfsumme falsch"
    assert tnef.decompress_rtf(mela(rtf)) == (rtf, None)
    assert tnef.decompress_rtf(b"\x10\x00\x00\x00\x10\x00\x00\x00ABCD\x00\x00\x00\x00")[0] is None
    assert tnef.decompress_rtf(b"kurz")[0] is None


def test_lzfu_bomb_is_capped_quickly():
    """Kompressionsbombe: Verweise mit Länge 17 ohne Endmarke; die Größe im Kopf behauptet 4 GB."""
    body, wp = bytearray(), 207
    for _ in range(200_000):
        body.append(0xFF)
        for _ in range(8):
            body += struct.pack(">H", (((wp + 2048) & 0xFFF) << 4) | 0xF)   # nie die Schreibposition (Endmarke)
            wp = (wp + 17) & 0xFFF
    body = bytes(body)
    data = struct.pack("<IIII", len(body) + 12, 0xFFFFFFFF, 0x75465A4C, 0) + body
    t0 = time.time()
    out, note = tnef.decompress_rtf(data)
    assert len(out) == tnef.MAX_RTF_BYTES and "gekürzt" in note
    assert time.time() - t0 < 15


def test_html_encapsulated_rtf_becomes_html_again():
    rtf = html_rtf(["Sehr geehrte Damen und Herren, die Rechnung RE-77 über 120 € liegt bei.",
                    "Mit freundlichen Grüßen"])
    kind, html = tnef.rtf_to_text(rtf)
    assert kind == "html" and "Rechnung RE-77 über 120 € liegt bei." in html
    assert "<p class=MsoNormal>" in html and "Arial" not in html and "htmlrtf" not in html
    msg = tnef.parse(tnef_bytes([], rtf=lzfu(rtf)))
    assert msg.text()[0] == "html" and "Mit freundlichen Grüßen" in msg.text()[1]


def test_text_encapsulated_and_native_rtf():
    fromtext = (b"{\\rtf1\\ansi\\ansicpg1252\\fromtext \\fbidis \\deff0{\\fonttbl{\\f0\\fswiss Arial;}}\r\n"
                b"{\\*\\generator Riched20;}\\htmlrtf {\\htmlrtf0 Z\\'e4hlerstand Keller 4711\\par\r\n"
                b"Gru\\'df\\par\r\n}}")
    assert tnef.rtf_to_text(fromtext) == ("text", "Zählerstand Keller 4711\nGruß\n")
    native = (b"{\\rtf1\\ansi\\ansicpg1252{\\fonttbl{\\f0 Arial;}}{\\colortbl;\\red255\\green0\\blue0;}"
              b"{\\*\\expandedcolortbl;;}\\pard Hausordnung \\b Ruhezeiten\\b0\\par "
              b"{\\field{\\*\\fldinst HYPERLINK \"https://x.example\"}{\\fldrslt Link}}\\tab \\u8364?5 "
              b"\\uc0\\u-10179\\u-8704 Ende\\par}")
    kind, text = tnef.rtf_to_text(native)
    assert kind == "text" and text == "Hausordnung Ruhezeiten\nLink\t€5 \U0001f600Ende\n"   # Leerzeichen trennt


def test_text_priority_body_html_rtf():
    rtf = lzfu(b"{\\rtf1\\ansi\\fromtext Aus dem RTF\\par}")
    assert tnef.parse(tnef_bytes([], body="Klartext zuerst", rtf=rtf)).text() == ("text", "Klartext zuerst")
    html = "<p>Aus dem HTML Grüße</p>".encode("cp1252")
    assert tnef.parse(tnef_bytes([], html=html, rtf=rtf)).text() == ("html", "<p>Aus dem HTML Grüße</p>")
    assert tnef.parse(tnef_bytes([], rtf=rtf)).text() == ("text", "Aus dem RTF\n")
    assert tnef.parse(tnef_bytes([])).text() is None


def test_rtf_with_wrong_crc_reports_on_the_message():
    comp = bytearray(lzfu(b"{\\rtf1\\ansi\\fromtext Text\\par}"))
    comp[12] ^= 1
    msg = tnef.parse(tnef_bytes([], rtf=bytes(comp)))
    assert msg.text() == ("text", "Text\n") and msg.notes == ["RTF-Mailtext: Prüfsumme falsch"]


def test_big_stream_is_fast():
    """P1 (großer Strom): 8,5 MB mit einem 4,5-MB-Foto und einem großen PDF – Lesen samt Prüfsummen in < 1 s."""
    big_jpeg = b"\xff\xd8\xff\xe0" + random.Random(1).randbytes(4_500_000)
    big_pdf = b"%PDF-1.4\n" + random.Random(2).randbytes(3_900_000)
    data = tnef_bytes([tnef_attachment(big_jpeg, "A" * 80 + ".jpg", variant="C"),
                       tnef_attachment(big_pdf, "Sammelrechnung 96 Seiten.pdf", metafile=True, dates=True)])
    t0 = time.time()
    msg = tnef.parse(data)
    assert time.time() - t0 < 1.0
    assert [len(a.data) for a in msg.attachments] == [len(big_jpeg), len(big_pdf)] and msg.notes == []


def test_object_property_with_unknown_iid_is_plain_data():
    a = tnef_attachment(None, "obj.bin", extra=[(PT_OBJECT, 0x3701, bytes(16) + PDF)])
    att = tnef.parse(tnef_bytes([a])).attachments[0]
    assert att.data == PDF and not att.embedded and not att.ole


def test_string8_message_body_uses_message_codepage():
    data = tnef_bytes([], codepage=1252, extra_props=[(PT_STRING8, 0x1000, "Kündigung zum 31.12.".encode("cp1252"))])
    assert tnef.parse(data).body == "Kündigung zum 31.12."


# --- Nachbesserung nach der Prüfung --------------------------------------------------------------------------

def _prop_lists(n_lists: int, per_list: int, level: int = 1) -> bytes:
    """attMsgProps (bzw. attAttachment) mit je per_list Eigenschaften PT_LONG, die der Leser nicht braucht."""
    lst = struct.pack("<I", per_list) + struct.pack("<HHI", PT_LONG, 0x0E21, 7) * per_list
    return tnef_attr(level, 0x00069003 if level == 1 else 0x00069005, lst) * n_lists


def test_crafted_property_lists_stay_fast_and_the_attachment_is_still_read():
    """Präparierte 50-MB-Ströme (Listen mit 20.000 Eigenschaften, Mehrfachwerte fester und variabler Größe): das
    Budget gilt für alle Listen des Stroms, Werte fester Größe werden in einem Schritt übersprungen – unter 1 s je
    Strom. Der Anhang dahinter wird trotzdem gelesen."""
    att = tnef_attachment(PDF, "Rechnung.pdf")
    many = SIG_KEY + _prop_lists(320, 19_000)                                    # 6 Mio. Eigenschaften
    mv_fixed = struct.pack("<IHHI", 1, 0x0002 | MV, 0x0E22, 100_000) + bytes(4 * 100_000)
    mv_var = struct.pack("<IHHI", 1, PT_BINARY | MV, 0x0E23, 100_000) + struct.pack("<I", 0) * 100_000
    for data, budget_hit in ((many, True), (SIG_KEY + tnef_attr(1, 0x00069003, mv_fixed) * 125, False),
                             (SIG_KEY + tnef_attr(1, 0x00069003, mv_var) * 125, True)):
        assert len(data) > 40_000_000
        t0 = time.time()
        msg = tnef.parse(data + att)
        assert time.time() - t0 < 1.0
        assert (tnef.BUDGET_NOTE in msg.notes) == budget_hit and msg.notes.count(tnef.BUDGET_NOTE) <= 1
        assert [a.data for a in msg.attachments] == [PDF] and msg.attachments[0].name == "Rechnung.pdf"


def test_budget_is_shared_when_given():
    """Ein Budget für mehrere Ströme (alle winmail.dat einer Mail): ist es aufgebraucht, fehlen nur die Eigenschaften
    (Name dann aus attAttachTitle), Anhänge und ihre Daten bleiben."""
    a, b = tnef_bytes([tnef_attachment(PDF, "Plan Süd.pdf")]), tnef_bytes([tnef_attachment(JPEG, "Foto Süd.jpg")])
    probe = tnef.Budget()
    tnef.parse(a, probe)
    used = tnef.MAX_PROP_WORK - probe.left
    assert 0 < used < 100                                       # echte Ströme brauchen wenig
    budget = tnef.Budget(used + used // 2)
    first = tnef.parse(a, budget)
    assert first.attachments[0].name == "Plan Süd.pdf" and first.notes == []
    second = tnef.parse(b, budget)
    assert second.notes == [tnef.BUDGET_NOTE] and second.attachments[0].data == JPEG
    assert second.attachments[0].name == "Foto S?d.jpg"         # Titel (OEM-Codepage, Umlaut als „?“)
    assert tnef.parse(b).attachments[0].name == "Foto Süd.jpg"  # ohne Angabe: eigenes Budget je Strom


def test_parse_props_wanted_skips_others_without_changing_positions():
    lst = mapi_props([(PT_LONG | MV, 0x0E21, [1, 2, 3]), (PT_BINARY, 0x0FF9, b"abcde"), (0x0002, 0x0E22, 5),
                      (PT_UNICODE, 0x3707, "Plan.pdf"), (PT_BINARY | MV, 0x0E23, [b"x", b"yz"]),
                      (PT_LONG, 0x3705, 1)])
    props, note = tnef.parse_props(lst, frozenset({0x3707, 0x3705}))
    assert note is None and [(p[1], p[2]) for p in props] == [
        (0x3707, ["Plan.pdf\x00".encode("utf-16-le")]), (0x3705, [struct.pack("<I", 1)])]
    assert [p[1] for p in tnef.parse_props(lst)[0]] == [0x0E21, 0x0FF9, 0x0E22, 0x3707, 0x0E23, 0x3705]


def test_huge_names_are_cut_keeping_start_and_extension():
    big = "N" * 1_000_000 + "ä.pdf"
    att = tnef.parse(tnef_bytes([tnef_attachment(PDF, big, mime="x/" + "y" * 500_000, variant="B")])).attachments[0]
    assert len(att.long_name) == tnef.MAX_NAME_CHARS and att.long_name.endswith("Nä.pdf")
    assert len(att.mime) == tnef.MAX_NAME_CHARS and att.mime.startswith("x/y") and att.data == PDF


def test_unassigned_windows_bytes_are_dropped_not_replaced():
    """In Windows-1252 nicht belegte Bytes (0x81, 0x8D, 0x8F, 0x90, 0x9D) – im RTF als \\'xx neben Symbolen –
    werden weggelassen statt „�“; in Namen (PT_STRING8) genauso."""
    rtf = b"{\\rtf1\\ansi\\ansicpg1252\\fromtext Stra\\'dfe \\'81\\'8d\\'8f\\'90\\'9d Ende\\par}"
    kind, text = tnef.rtf_to_text(rtf)
    assert text == "Straße  Ende\n" and "�" not in text
    a = tnef_attachment(PDF, "x.pdf", variant="C", extra=[(PT_STRING8, 0x3707, b"Pl\x81an.pdf")])
    assert tnef.parse(tnef_bytes([a], codepage=1252)).attachments[0].name == "Plan.pdf"
    assert tnef.decode_cp(b"\x81", "cp1251") == "Ѓ"                 # andere Codepage: dort belegt
    assert tnef.decode_cp(b"\xff\xfe", "utf-8") == "��"        # nur Windows-125x: sonst bleibt „�“
