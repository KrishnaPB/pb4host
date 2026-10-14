import httpx
import pytest
import respx

from prospector.connectors.base import CapExceeded, ConnectorError, TermsReviewRequired
from prospector.connectors.google_places import TEXT_SEARCH_URL, GooglePlacesConnector
from prospector.models import ConnectorState
from tests.conftest import json_fixture


@pytest.fixture()
def places(settings, monkeypatch):
    monkeypatch.setattr(settings, "google_maps_api_key", "test-key")
    return GooglePlacesConnector(settings, client=httpx.Client())


@respx.mock
def test_text_search_sends_field_mask_and_persists_only_allowed_fields(session, places):
    route = respx.post(TEXT_SEARCH_URL).mock(return_value=httpx.Response(200, json=json_fixture("places_search.json")))
    page = places.search_text(session, "bakery in Mangaluru", track="smb", region_code="IN")
    sent = route.calls.last.request
    assert sent.headers["X-Goog-Api-Key"] == "test-key"
    assert "places.websiteUri" in sent.headers["X-Goog-FieldMask"]
    assert "nationalPhoneNumber" not in sent.headers["X-Goog-FieldMask"]
    assert page.raw_count == 4 and page.without_website == 1 and page.next_page_token == "NEXT_PAGE_TOKEN_1"
    assert len(page.candidates) == 3
    first = page.candidates[0]
    assert first.external_id == "ChIJdemo0000000000000001"
    # storage policy: only place_id + coords (+ expiry); nothing else from Google content is persisted
    assert set(first.persist) <= {"place_id", "lat", "lng", "coords_expires_at"}
    assert first.persist["place_id"] == first.external_id and first.persist["coords_expires_at"] is not None
    assert first.website_hint == "https://coastalbakes.example/"
    assert "nationalPhoneNumber" not in str(first.persist) and "Coastal Bakes" not in str(first.persist)
    agency = page.candidates[1]
    assert agency.transient["agency_type_hint"] is True
    closed = page.candidates[2]
    assert closed.transient["operational"] is False
    state = session.query(ConnectorState).filter_by(name="google_places").one()
    assert state.calls_today == 1


@respx.mock
def test_http_error_records_connector_error(session, places):
    respx.post(TEXT_SEARCH_URL).mock(return_value=httpx.Response(403, json={"error": {"message": "denied"}}))
    with pytest.raises(ConnectorError):
        places.search_text(session, "x")
    state = session.query(ConnectorState).filter_by(name="google_places").one()
    assert state.last_error and "403" in state.last_error


def test_terms_review_gate_and_daily_cap(session, places):
    state = session.query(ConnectorState).filter_by(name="google_places").one()
    state.terms_reviewed_at = None
    session.commit()
    with pytest.raises(TermsReviewRequired):
        places.search_text(session, "x")
    from prospector.util import utcnow

    state.terms_reviewed_at = utcnow()
    state.daily_cap = 1
    state.calls_today = 1
    state.calls_date = utcnow().date()
    session.commit()
    with pytest.raises(CapExceeded):
        places.search_text(session, "x")


def test_missing_api_key_is_a_clear_error(session, settings):
    places = GooglePlacesConnector(settings, client=httpx.Client())
    with pytest.raises(ConnectorError, match="GOOGLE_MAPS_API_KEY"):
        places.search_text(session, "x")


@respx.mock
def test_hosted_profile_website_hint_counts_as_no_website(session, places):
    from prospector.api.routers.runs import create_run
    from prospector.jobs import tasks
    from prospector.models import DiscoveryRun, Organization

    data = json_fixture("places_search.json")
    data["places"][0]["websiteUri"] = "https://www.facebook.com/coastalbakes"
    data["places"][1]["websiteUri"] = "https://linktr.ee/pixelgrove"
    data["nextPageToken"] = None
    respx.post(TEXT_SEARCH_URL).mock(return_value=httpx.Response(200, json=data))
    tasks.overrides["google_places"] = places
    try:
        run = create_run(session, query="bakery", location=None, track="smb", max_pages=1, region_code="IN", user=None)
        session.commit()
        tasks.places_text_search(session, {"run_id": run.id, "page_no": 1})
    finally:
        tasks.overrides.clear()
    run = session.get(DiscoveryRun, run.id)
    assert run.candidates_without_website == 3 and run.organizations_created == 1
    orgs = session.query(Organization).all()
    assert [o.domain for o in orgs] == ["closedcafe.example"]
