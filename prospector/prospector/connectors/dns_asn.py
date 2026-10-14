"""DNS / IP → ASN enrichment: mail provider, hosting provider and class.

ASN mapping uses Team Cymru's DNS-based IP-to-ASN service (TXT lookups), which is free for
reasonable, non-bulk use — hence the terms-review requirement and daily cap.
"""

from __future__ import annotations

from dataclasses import dataclass

import dns.exception
import dns.resolver

from ..core.facts import Fact
from ..util import utcnow
from .base import Connector, register

MAIL_PROVIDERS = [
    ("google_workspace", ("google.com", "googlemail.com")),
    ("microsoft_365", ("outlook.com", "protection.outlook.com")),
    ("zoho", ("zoho.com", "zoho.in", "zohomail.com")),
    ("godaddy", ("secureserver.net",)),
    ("hostinger", ("hostinger.com", "titan.email")),
    ("rediff", ("rediffmailpro.com", "rediffmail.com")),
    ("yandex", ("yandex.net", "yandex.ru")),
    ("proton", ("protonmail.ch", "proton.me")),
    ("mimecast", ("mimecast.com",)),
    ("proofpoint", ("pphosted.com",)),
    ("ovh", ("ovh.net",)),
    ("ionos", ("ionos.com", "kundenserver.de", "1and1.com")),
]
HOST_CLASSES = [
    ("managed_wp", ("wpengine", "wp engine", "kinsta", "flywheel", "pantheon", "pressable", "automattic", "wordpress.com")),
    ("cdn_proxied", ("cloudflare", "fastly", "akamai", "sucuri", "incapsula", "imperva", "bunny")),
    ("shared", ("hostinger", "godaddy", "bluehost", "newfold", "endurance", "hostgator", "namecheap",
                "bigrock", "siteground", "a2 hosting", "a2hosting", "ionos", "1&1", "dreamhost",
                "inmotion", "hostpapa", "hostwinds", "milesweb", "hostitsmart", "znetlive", "unified layer",
                "websitewelcome", "hostdime", "liquid web", "hostmonster", "justhost", "resellerclub",
                "publicdomainregistry", "webwerks", "ctrls", "netmagic", "web werks", "hostinger international")),
    ("cloud", ("amazon", "aws", "google", "microsoft", "azure", "digitalocean", "hetzner", "linode",
               "akamai connected cloud", "vultr", "ovh", "contabo", "oracle", "upcloud", "scaleway",
               "alibaba", "tencent", "ibm", "e2e networks", "yotta")),
]


@dataclass
class DnsResult:
    host: str
    ip: str | None = None
    asn: str | None = None
    as_name: str | None = None
    as_country: str | None = None
    mail_provider: str | None = None
    mx_hosts: list[str] | None = None
    host_class: str | None = None
    error: str | None = None


class DnsClient:
    """Thin wrapper so tests can inject canned answers."""

    def __init__(self, timeout: float = 4.0) -> None:
        self.resolver = dns.resolver.Resolver()
        self.resolver.timeout = timeout
        self.resolver.lifetime = timeout * 2

    def query(self, name: str, rdtype: str) -> list[str]:
        try:
            answers = self.resolver.resolve(name, rdtype)
        except (dns.exception.DNSException, OSError):
            return []
        out = []
        for rdata in answers:
            if rdtype == "TXT":
                out.append("".join(s.decode() if isinstance(s, bytes) else s for s in rdata.strings))
            elif rdtype == "MX":
                out.append(str(rdata.exchange).rstrip(".").lower())
            else:
                out.append(str(rdata))
        return out


def classify_host(as_name: str | None) -> str | None:
    if not as_name:
        return None
    lowered = as_name.lower()
    for cls, keys in HOST_CLASSES:
        if any(k in lowered for k in keys):
            return cls
    return "unknown"


def mail_provider_for(mx_hosts: list[str], own_domain: str) -> str | None:
    if not mx_hosts:
        return None
    for mx in mx_hosts:
        for name, suffixes in MAIL_PROVIDERS:
            if any(mx == s or mx.endswith("." + s) for s in suffixes):
                return name
    if any(mx == own_domain or mx.endswith("." + own_domain) for mx in mx_hosts):
        return "self_hosted"
    return "other"


@register
class DnsAsnConnector(Connector):
    name = "dns_asn"
    terms_url = "https://www.team-cymru.com/ip-asn-mapping"
    requires_terms_review = True
    default_daily_cap = 5000
    cost_per_call_usd = 0.0

    def __init__(self, settings=None, client: DnsClient | None = None) -> None:
        super().__init__(settings)
        self.client = client or DnsClient()

    def lookup(self, session, host: str, registered: str | None = None) -> DnsResult:
        self.reserve_calls(session, 1)
        result = DnsResult(host=host)
        ips = self.client.query(host, "A")
        if not ips:
            result.error = "no_a_record"
        else:
            result.ip = ips[0]
            reversed_ip = ".".join(reversed(result.ip.split(".")))
            txt = self.client.query(f"{reversed_ip}.origin.asn.cymru.com", "TXT")
            if txt:
                parts = [p.strip() for p in txt[0].split("|")]
                if parts and parts[0]:
                    result.asn = parts[0].split()[0]
                    if len(parts) > 2:
                        result.as_country = parts[2] or None
                    name_txt = self.client.query(f"AS{result.asn}.asn.cymru.com", "TXT")
                    if name_txt:
                        name_parts = [p.strip() for p in name_txt[0].split("|")]
                        result.as_name = name_parts[-1] if name_parts else None
            result.host_class = classify_host(result.as_name)
        domain = registered or host
        result.mx_hosts = self.client.query(domain, "MX")
        result.mail_provider = mail_provider_for(result.mx_hosts, domain)
        return result

    def facts(self, result: DnsResult, source_url: str) -> list[Fact]:
        now = utcnow()
        facts: list[Fact] = []
        if result.ip:
            facts.append(Fact("website.host_ip", result.ip, "dns", source_url, 1.0, now))
        if result.asn:
            facts.append(Fact("website.host_asn", f"AS{result.asn}", "dns", source_url, 1.0, now))
        if result.as_name:
            facts.append(Fact("website.host_provider", result.as_name, "dns", source_url, 0.9, now))
        if result.host_class:
            facts.append(Fact("website.host_class", result.host_class, "dns", source_url, 0.7, now))
        if result.mail_provider:
            facts.append(Fact("website.mail_provider", result.mail_provider, "dns", source_url, 0.9, now))
        return self.check_facts(facts)
