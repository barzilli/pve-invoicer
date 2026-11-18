from pve_invoicer.notes import parse_notes

GOOD = """Web shop server.

```billing
customer_id: ACME
owner: Acme Ltd
email: billing@acme.example
cc: [ops@acme.example]
address: |
  1 Main Street
  Springfield 12345
po_number: PO-1
```

Other notes here.
"""


def test_valid_block():
    r = parse_notes(GOOD)
    assert r.ok and not r.warnings
    assert r.info.customer_id == "ACME"
    assert r.info.email == "billing@acme.example"
    assert r.info.cc == ("ops@acme.example",)
    assert r.info.address == "1 Main Street\nSpringfield 12345"
    assert r.info.po_number == "PO-1"


def test_tilde_fence_and_cc_string_and_unknown_key():
    r = parse_notes("~~~billing\ncustomer_id: B-2\nemail: a@b.example\ncc: x@b.example, y@b.example\nprice: 0\n~~~")
    assert r.ok
    assert r.info.cc == ("x@b.example", "y@b.example")
    assert r.warnings == ["unknown keys ignored: price"]


def test_missing_notes_and_block():
    assert parse_notes(None).errors == ["no notes"]
    assert parse_notes("just text").errors == ["no ```billing block in notes"]


def test_required_fields_and_bad_email():
    r = parse_notes("```billing\nowner: X\nemail: not-an-email\n```")
    assert not r.ok
    assert "missing customer_id" in r.errors
    assert any("email is not a valid address" in e for e in r.errors)


def test_two_blocks_rejected():
    block = "```billing\ncustomer_id: A\nemail: a@b.example\n```\n"
    assert parse_notes(block + block).errors == ["more than one ```billing block in notes"]


def test_invalid_yaml_and_non_mapping():
    assert "not valid YAML" in parse_notes("```billing\ncustomer_id: [\n```").errors[0]
    assert parse_notes("```billing\n- a\n- b\n```").errors == ["billing block must be key: value pairs"]


def test_bad_customer_id():
    r = parse_notes("```billing\ncustomer_id: 'a b'\nemail: a@b.example\n```")
    assert not r.ok and "customer_id may only contain" in r.errors[0]
