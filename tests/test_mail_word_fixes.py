"""Synthetic regressions for textual Word attachments, XML encodings and flowed mail text."""
from __future__ import annotations

from email import policy
from email.message import EmailMessage
from xml.etree import ElementTree as ET

import pytest

from belegsuche import mail, word
from belegsuche.extract import extract_full
from belegsuche.normalize import index_text


TEXT = 'Hausverwaltung Müller und Söhne: Kaltmiete 650,00 EUR'
WORDML_NS = 'http://schemas.microsoft.com/office/word/2003/wordml'


def wordml(encoding='UTF-8', *, actual_encoding=None):
    xml = (f'<?xml version="1.0" encoding="{encoding}"?>'
           f'<w:wordDocument xmlns:w="{WORDML_NS}"><w:body><w:p><w:r><w:t>{TEXT}</w:t>'
           '</w:r></w:p></w:body></w:wordDocument>')
    return xml.encode(actual_encoding or encoding)


TEXT_DOCS = [
    f'<html><head><title>Bescheinigung</title></head><body><p>{TEXT}</p></body></html>'.encode(),
    wordml(),
    TEXT.encode('cp1252'),
]


def attachment_mail(tmp_path, data, *, filename='Bescheinigung.doc', subtype='msword'):
    msg = EmailMessage(policy=policy.SMTP)
    msg['From'] = 'verwaltung@beispiel.example'
    msg['To'] = 'buchhaltung@beispiel.example'
    msg['Subject'] = 'Mietbescheinigung'
    msg.set_content('Anbei die angefragte Bescheinigung.')
    msg.add_attachment(data, maintype='application', subtype=subtype, filename=filename)
    path = tmp_path / 'bescheinigung.eml'
    path.write_bytes(msg.as_bytes())
    return path


@pytest.mark.parametrize('data', TEXT_DOCS, ids=['html', 'wordml', 'plain'])
@pytest.mark.parametrize('subtype', ['msword', 'octet-stream'])
def test_supported_text_doc_attachment_matches_standalone(tmp_path, data, subtype):
    source = tmp_path / 'Bescheinigung.doc'
    source.write_bytes(data)
    path = attachment_mail(tmp_path, data, subtype=subtype)
    result = extract_full(path, None)
    assert result.parts[1].kind == 'word'
    assert result.parts[1].n_pages == 1 and result.parts[1].note is None
    assert result.pages[1].text == extract_full(source, None).pages[0].text == TEXT
    assert source.read_bytes() == data
    assert mail.find_part(path.read_bytes(), result.parts[1].mime_path) == data


@pytest.mark.parametrize('encoding', ['UTF-16', 'utf-16', 'uTf-16', 'windows-1252', 'UTF-8'])
def test_wordml_encoding_uses_original_bytes(encoding):
    data = wordml(encoding)
    assert ''.join(ET.fromstring(data).itertext()) == TEXT
    assert word.word_pages(data, [])[0].text == TEXT


def test_wordml_utf16_big_endian_preserves_text():
    data = b'\xfe\xff' + wordml('utf-16', actual_encoding='utf-16-be')
    assert word.word_pages(data, [])[0].text == TEXT


@pytest.mark.parametrize('data,kind', list(zip(TEXT_DOCS, ['html', 'wordml', 'text'])))
def test_text_word_kind_recognizes_supported_content(data, kind):
    assert word.text_word_kind(data) == kind


def test_text_word_recognition_decodes_only_bounded_prefix(monkeypatch):
    original = word._decode_text
    lengths = []

    def decode(data):
        lengths.append(len(data))
        return original(data)

    monkeypatch.setattr(word, '_decode_text', decode)
    assert word.text_word_kind(b'Mietbescheinigung 650 EUR\n' * 1000) == 'text'
    assert lengths == [8192]


def test_word_byte_input_limit_applies_before_parsing(monkeypatch):
    monkeypatch.setattr(word, 'MAX_WORD_BYTES', 32)
    with pytest.raises(word.ExtractError, match='zu groß'):
        word.word_pages(TEXT_DOCS[0], [])


@pytest.mark.parametrize('data', [
    b'<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"><Worksheet/></Workbook>',
    b'<html><head><meta name="ProgId" content="Excel.Sheet"></head><body><table><tr><td>650 EUR</td></tr></table></body></html>',
    b'<html><head><meta name=ProgId content=Excel.Sheet></head><body><table><tr><td>650 EUR</td></tr></table></body></html>',
    b'<html xmlns:x="urn:schemas-microsoft-com:office:excel"><head><xml><x:ExcelWorkbook/></xml></head><body>650 EUR</body></html>',
    b'<?xml version="1.0"?><rechnung><betrag>650 EUR</betrag></rechnung>',
    b'\x00\x01\x02\x03\x04',
])
def test_text_word_kind_does_not_admit_excel_or_unrelated_xml(tmp_path, data):
    assert word.text_word_kind(data) is None
    result = extract_full(attachment_mail(tmp_path, data), None)
    assert result.parts[1].kind == 'skipped' and result.parts[1].n_pages == 0


@pytest.mark.parametrize('filename,subtype', [
    ('Bescheinigung.txt', 'octet-stream'),
    ('Bescheinigung.docx', 'octet-stream'),
    ('Bescheinigung.xlsx', 'octet-stream'),
])
def test_textual_word_fallback_does_not_trust_other_extensions(tmp_path, filename, subtype):
    result = extract_full(attachment_mail(tmp_path, TEXT_DOCS[0], filename=filename, subtype=subtype), None)
    assert result.parts[1].kind != 'word'


def test_word_mime_without_filename_can_read_text_doc(tmp_path):
    result = extract_full(attachment_mail(tmp_path, TEXT_DOCS[0], filename=None), None)
    assert result.parts[1].kind == 'word' and result.pages[1].text == TEXT


@pytest.mark.parametrize('attachment_limit,text_limit,note', [(64, 32, 'Textdatei zu groß'), (32, 64, 'zu groß')])
def test_text_word_attachment_size_limits_run_before_recognition(tmp_path, monkeypatch, attachment_limit,
                                                               text_limit, note):
    data = b'Mietbescheinigung Kaltmiete 650 EUR'
    monkeypatch.setattr(mail, 'MAX_ATTACHMENT_BYTES', attachment_limit)
    monkeypatch.setattr(mail, 'MAX_TEXT_ATTACHMENT_BYTES', text_limit)

    def should_not_read(data):
        pytest.fail('oversized textual Word payload must not be inspected')

    monkeypatch.setattr(word, 'text_word_kind', should_not_read)
    result = extract_full(attachment_mail(tmp_path, data), None)
    assert result.parts[1].kind == 'skipped' and note in result.parts[1].note


def plain_part(text, *, fmt='flowed', delsp='yes'):
    part = EmailMessage()
    part.set_content(text)
    if fmt is not None:
        part.set_param('format', fmt)
    if delsp is not None:
        part.set_param('delsp', delsp)
    return part


@pytest.mark.parametrize('text,delsp,expected', [
    ('Rechnungsnummer BE-20 \n26-4711\n', 'yes', 'Rechnungsnummer BE-2026-4711\n'),
    ('Kessel \nwartung\n', 'yes', 'Kesselwartung\n'),
    ('Wartung \nHeizkessel\n', 'no', 'Wartung Heizkessel\n'),
    ('Wartung \nHeizkessel\n', None, 'Wartung Heizkessel\n'),
    ('Wartung \nHeizkessel\n', 'unknown', 'Wartung Heizkessel\n'),
    ('Wartung  \nHeizkessel\n', 'yes', 'Wartung Heizkessel\n'),
    ('> Kessel \n> wartung\n', 'yes', '> Kesselwartung\n'),
    ('>> Kessel \n>> wartung\n', 'yes', '>> Kesselwartung\n'),
    ('> Erster Absatz \n>> Andere Zitiertiefe\n', 'yes', '> Erster Absatz\n>> Andere Zitiertiefe\n'),
    ('> Erster Absatz \nOhne Zitat\n', 'yes', '> Erster Absatz\nOhne Zitat\n'),
    ('Wartung \n-- \nHausverwaltung\n', 'yes', 'Wartung\n-- \nHausverwaltung\n'),
    ('> Wartung \n> -- \n> Hausverwaltung\n', 'yes', '> Wartung\n> -- \n> Hausverwaltung\n'),
    (' From Hausverwaltung\n >wörtlicher Pfeil\n  eingerückter Text\n', 'yes',
     'From Hausverwaltung\n>wörtlicher Pfeil\n eingerückter Text\n'),
    ('Rechnung ohne weichen Umbruch\nNeue feste Zeile\n', 'yes',
     'Rechnung ohne weichen Umbruch\nNeue feste Zeile\n'),
])
def test_flowed_body_parameters_and_structure(text, delsp, expected):
    assert mail.part_text(plain_part(text, delsp=delsp)) == expected


@pytest.mark.parametrize('fmt', [None, 'fixed', 'unknown'])
def test_fixed_plain_text_does_not_unfold_or_unstuff(fmt):
    text = ' From Hausverwaltung \nNeue Zeile\n-- \nSignatur\n'
    assert mail.part_text(plain_part(text, fmt=fmt)) == text


def test_flowed_parameter_values_are_case_insensitive():
    assert mail.part_text(plain_part('Kessel \nwartung\n', fmt='FlOwEd', delsp='YeS')) == 'Kesselwartung\n'


def test_flowed_mail_populates_correct_number_and_word_tokens(tmp_path):
    msg = plain_part('Rechnungsnummer BE-20 \n26-4711\nLeistung: Kessel \nwartung\n')
    msg['From'] = 'service@beispiel.example'
    msg['To'] = 'buchhaltung@beispiel.example'
    msg['Subject'] = 'Wartungsauftrag'
    path = tmp_path / 'flowed.eml'
    path.write_bytes(msg.as_bytes())
    text = extract_full(path, None).pages[0].text
    tokens = index_text(text).split()
    assert '0idbe20264711' in tokens and 'kesselwartung' in tokens


def test_attached_flowed_text_uses_the_same_logical_lines(tmp_path):
    msg = EmailMessage(policy=policy.SMTP)
    msg.set_content('Anbei das Protokoll.')
    part = plain_part('Kessel \nwartung\n', delsp='yes')
    part.add_header('Content-Disposition', 'attachment', filename='Protokoll.txt')
    msg.make_mixed()
    msg.attach(part)
    path = tmp_path / 'bericht.eml'
    path.write_bytes(msg.as_bytes())
    assert extract_full(path, None).pages[1].text == 'Kesselwartung'
