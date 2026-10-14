from prospector.connectors.dns_asn import DnsAsnConnector, classify_host, mail_provider_for


class FakeDns:
    def __init__(self, answers):
        self.answers = answers

    def query(self, name, rdtype):
        return self.answers.get((name, rdtype), [])


def test_lookup_maps_asn_mail_and_host_class(session, settings):
    dns = FakeDns({
        ("coastalbakes.example", "A"): ["203.0.113.10"],
        ("10.113.0.203.origin.asn.cymru.com", "TXT"): ["47583 | 203.0.113.0/24 | IN | apnic | 2019-01-01"],
        ("AS47583.asn.cymru.com", "TXT"): ["47583 | IN | apnic | 2019-01-01 | AS-HOSTINGER, Hostinger International Limited, CY"],
        ("coastalbakes.example", "MX"): ["aspmx.l.google.com", "alt1.aspmx.l.google.com"],
    })
    conn = DnsAsnConnector(settings, client=dns)
    result = conn.lookup(session, "coastalbakes.example", "coastalbakes.example")
    assert result.ip == "203.0.113.10" and result.asn == "47583"
    assert "Hostinger" in result.as_name and result.host_class == "shared"
    assert result.mail_provider == "google_workspace"
    facts = {f.field: f.value for f in conn.facts(result, "dns://coastalbakes.example")}
    assert facts["website.host_asn"] == "AS47583" and facts["website.mail_provider"] == "google_workspace"
    assert all(f.source == "dns" for f in conn.facts(result, "dns://coastalbakes.example"))


def test_classifiers():
    assert classify_host("CLOUDFLARENET, US") == "cdn_proxied"
    assert classify_host("AMAZON-02, US") == "cloud"
    assert classify_host("WPENGINE, US") == "managed_wp"
    assert classify_host("Some Local ISP") == "unknown"
    assert mail_provider_for(["mx1.example.in"], "example.in") == "self_hosted"
    assert mail_provider_for(["example-in.mail.protection.outlook.com"], "example.in") == "microsoft_365"
    assert mail_provider_for([], "example.in") is None
