import httpx
import pytest
import respx

from prospector.connectors.base import ConnectorError
from prospector.connectors.pagespeed import PSI_URL, PageSpeedConnector, median_of, parse_response
from tests.conftest import json_fixture


def test_parse_psi_response():
    res = parse_response(json_fixture("psi_mobile.json"), "mobile")
    assert res.lh_performance == 31
    assert res.lcp_ms == 6214 and res.tbt_ms == 842 and res.cls == 0.241 and res.ttfb_ms == 1340
    assert res.field_available and res.field_origin_fallback
    assert res.field_lcp_p75 == 4850 and res.field_inp_p75 == 612 and res.field_cls_p75 == 0.12
    assert res.lighthouse_version == "12.6.0"
    assert res.raw["audits"]["largest-contentful-paint"] == 6214.5


def test_median_of_runs():
    a = parse_response(json_fixture("psi_mobile.json"), "mobile")
    b = parse_response(json_fixture("psi_mobile.json"), "mobile")
    b.lh_performance, b.lcp_ms = 51, 3000
    c = parse_response(json_fixture("psi_mobile.json"), "mobile")
    c.lh_performance, c.lcp_ms = 40, 4000
    m = median_of([a, b, c])
    assert m.runs == 3 and m.lh_performance == 40 and m.lcp_ms == 4000


@respx.mock
def test_run_counts_calls_and_passes_strategy(session, settings):
    route = respx.get(PSI_URL).mock(return_value=httpx.Response(200, json=json_fixture("psi_mobile.json")))
    psi = PageSpeedConnector(settings, client=httpx.Client())
    res = psi.run(session, "https://coastalbakes.example/", strategy="mobile")
    assert res.lh_performance == 31
    params = dict(route.calls.last.request.url.params)
    assert params["strategy"] == "mobile" and params["url"] == "https://coastalbakes.example/" and params["category"] == "performance"


@respx.mock
def test_run_failure_raises(session, settings):
    respx.get(PSI_URL).mock(return_value=httpx.Response(500, text="boom"))
    psi = PageSpeedConnector(settings, client=httpx.Client())
    with pytest.raises(ConnectorError):
        psi.run(session, "https://coastalbakes.example/")
