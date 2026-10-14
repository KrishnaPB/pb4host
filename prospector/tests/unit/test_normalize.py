from prospector.core.normalize import (
    find_emails,
    find_phones,
    host_key,
    is_generic_mailbox,
    normalize_email,
    normalize_name,
    normalize_phone,
    normalize_url,
    registered_domain,
)


def test_normalize_url_and_host_key():
    assert normalize_url("WWW.Example.IN") == "https://www.example.in/"
    assert host_key("https://www.Example.in/path?utm=1") == "example.in"
    assert host_key("http://shop.example.co.uk") == "shop.example.co.uk"
    assert registered_domain("https://shop.example.co.uk/") == "example.co.uk"
    assert normalize_url("not a url at all") is None


def test_phone_normalisation_india():
    assert normalize_phone("0824-2456789", "IN") == "+918242456789"
    assert normalize_phone("98450 12345", "IN") == "+919845012345"
    assert normalize_phone("12345", "IN") is None
    found = find_phones("Call 0824 245 6789 or WhatsApp +91 98450 12345 today", "IN")
    assert set(found) == {"+918242456789", "+919845012345"}


def test_email_helpers():
    assert normalize_email("Hello@Example.IN ") == "hello@example.in"
    assert normalize_email("nope") is None
    assert is_generic_mailbox("info@example.in") and is_generic_mailbox("sales@example.in")
    assert not is_generic_mailbox("priya.nair@example.in")
    assert find_emails("mail hello@example.in or image@2x.png") == ["hello@example.in"]


def test_normalize_name():
    assert normalize_name("Coastal Bakes Private Limited") == normalize_name("Coastal Bakes Pvt. Ltd.")
    assert normalize_name("The Pixelgrove Agency") != ""
