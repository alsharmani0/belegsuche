"""Parser regressions with fictional TNEF/RTF inputs and temporary mail files."""
import pytest

from belegsuche import mail, tnef

from .conftest import (PT_LONG, PT_UNICODE, make_eml, tnef_attachment, tnef_bytes)


@pytest.mark.parametrize("codepage,encoding", [(1200, "utf-16-le"), (1201, "utf-16-be")])
@pytest.mark.parametrize("terminated", [False, True])
def test_binary_html_utf16_preserves_searchable_mail_body(tmp_path, codepage, encoding, terminated):
    expected = "Kündigung Müller März 2025"
    html = f"<p>{expected}</p>" + ("\x00" if terminated else "")
    stream = tnef_bytes([], html=html.encode(encoding), codepage=codepage,
                        extra_props=[(PT_LONG, tnef.PR_MESSAGE_CODEPAGE, 1252)])
    parsed = tnef.parse(stream)
    assert parsed.html == f"<p>{expected}</p>" and parsed.notes == []
    source = make_eml(tmp_path / "mail.eml", text="",
                      attachments=[("winmail.dat", stream, "application/ms-tnef")])
    assert expected in mail.extract_mail(source, None).pages[0].text


@pytest.mark.parametrize("mime", ["text/plain", "application/x-unknown"])
def test_tnef_html_filename_survives_display_label_and_wrong_mime(tmp_path, mime):
    data = b"<p>Ventiltausch im <b>Birkenhof</b></p>"
    stream = tnef_bytes([tnef_attachment(data, "NOTIZ.HTML", variant="C", extra=[
        (PT_UNICODE, tnef.PR_DISPLAY_NAME, "Leistungsbeschreibung"),
        (PT_UNICODE, tnef.PR_ATTACH_MIME_TAG, mime)])])
    source = make_eml(tmp_path / "mail.eml", attachments=[("winmail.dat", stream, "application/ms-tnef")])
    result = mail.extract_mail(source, None)
    assert result.parts[-1].name == "Leistungsbeschreibung"
    assert result.pages[-1].text == "Ventiltausch im Birkenhof"
    assert mail.find_part(source.read_bytes(), result.parts[-1].mime_path) == data


@pytest.mark.parametrize("payload", [b"{", b"}", b"xxx}Bogus{yyy", b"\\par \\u65?"])
def test_rtf_binary_picture_bytes_do_not_change_groups_or_visible_text(payload):
    rtf = b"{\\rtf1 Vorher {\\pict\\bin" + str(len(payload)).encode() + b" " + payload + b"} Nachher}"
    assert tnef.rtf_to_text(rtf) == ("text", "Vorher  Nachher")
