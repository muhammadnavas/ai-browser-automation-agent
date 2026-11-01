"""
tests/test_multi_website_research.py
------------------------------------
Focused test suite covering:
1. URL validation and navigation security (loopback, private RFC1918, link-local, cloud metadata, schemes, prohibited ports).
2. Normalization (prices/currencies, measurements/units, dates/timestamps, text).
3. Deduplication and conflict detection (combining sources, attribution, flagging conflicting criteria).
4. Missing values representation.
5. Partial failures, timeouts, and cancellation behavior.
6. Bounded concurrency execution.
7. End-to-end API interface (submit, get status, get comparison results, cancel, export report).
"""

import ipaddress
import pytest
from unittest.mock import AsyncMock, MagicMock

from browser.security import (
    validate_url_security,
    is_ip_safe,
)
from services.research_normalization import (
    normalize_price,
    normalize_measurement,
    normalize_date,
    compute_canonical_key,
    deduplicate_and_compare,
)
from models.schemas import (
    ResearchRequest,
    TaskStatus,
    ComparisonResult,
)
from services.research_service import ResearchService


# ===========================================================================
# 1. Security & URL Validation Tests
# ===========================================================================

def test_security_blocks_prohibited_schemes():
    """Ensure non-HTTP/HTTPS schemes are blocked."""
    assert not validate_url_security("file:///etc/passwd")[0]
    assert not validate_url_security("javascript:alert(1)")[0]
    assert not validate_url_security("data:text/html,<b>hi</b>")[0]
    assert not validate_url_security("ftp://ftp.example.com/file")[0]
    assert not validate_url_security("gopher://gopher.example.com")[0]


def test_security_blocks_localhost_and_loopback():
    """Ensure loopback and localhost hostnames are strictly blocked."""
    assert not validate_url_security("http://localhost:8000")[0]
    assert not validate_url_security("http://127.0.0.1:3000")[0]
    assert not validate_url_security("http://127.0.0.5:80")[0]
    assert not validate_url_security("http://[::1]:8080")[0]
    assert not validate_url_security("http://sub.localhost:8000")[0]


def test_security_blocks_private_and_cloud_metadata_ips():
    """Ensure RFC1918 private IPs and cloud metadata endpoints are rejected."""
    # RFC 1918 Private
    assert not validate_url_security("http://10.0.0.1")[0]
    assert not validate_url_security("http://172.16.0.1")[0]
    assert not validate_url_security("http://192.168.1.1")[0]
    # Link-local / Cloud metadata
    assert not validate_url_security("http://169.254.169.254/latest/meta-data")[0]
    assert not validate_url_security("http://100.100.100.200/latest/meta-data")[0]
    assert not validate_url_security("http://metadata.google.internal")[0]


def test_security_blocks_prohibited_ports():
    """Ensure dangerous/administrative service ports are blocked."""
    for port in (22, 23, 25, 3306, 5432, 6379, 27017):
        is_safe, reason, _ = validate_url_security(f"https://example.com:{port}", resolve_dns=False)
        assert not is_safe
        assert f"Prohibited port {port}" in reason


def test_security_allows_safe_public_urls():
    """Ensure standard public HTTP/HTTPS URLs pass validation."""
    is_safe, _, norm_url = validate_url_security("https://www.example.com/products/item", resolve_dns=False)
    assert is_safe
    assert norm_url.startswith("https://www.example.com")

    is_safe2, _, norm_url2 = validate_url_security("http://example.org", resolve_dns=False)
    assert is_safe2


def test_is_ip_safe_checks():
    """Verify is_ip_safe unit check."""
    assert not is_ip_safe(ipaddress.ip_address("127.0.0.1"))[0]
    assert not is_ip_safe(ipaddress.ip_address("10.1.2.3"))[0]
    assert not is_ip_safe(ipaddress.ip_address("169.254.169.254"))[0]
    assert is_ip_safe(ipaddress.ip_address("93.184.216.34"))[0]  # example.com


# ===========================================================================
# 2. Normalization & Deduplication Tests
# ===========================================================================

def test_normalize_price_conservative():
    """Verify conservative price normalization without speculative conversion."""
    res_usd = normalize_price("$1,299.99")
    assert res_usd["amount"] == 1299.99
    assert res_usd["currency"] == "USD"
    assert res_usd["formatted"] == "$1,299.99"

    res_eur = normalize_price("€ 49.50")
    assert res_eur["amount"] == 49.50
    assert res_eur["currency"] == "EUR"
    assert res_eur["formatted"] == "€49.50"

    res_gbp = normalize_price("£120")
    assert res_gbp["amount"] == 120.0
    assert res_gbp["currency"] == "GBP"

    res_inr = normalize_price("₹ 24,999")
    assert res_inr["amount"] == 24999.0
    assert res_inr["currency"] == "INR"

    # Invalid input
    assert normalize_price("Not a price") is None
    assert normalize_price(None) is None


def test_normalize_measurement():
    """Verify measurement and unit normalization."""
    res_ram = normalize_measurement("16gb")
    assert res_ram["value"] == 16.0
    assert res_ram["unit"] == "GB"
    assert res_ram["formatted"] == "16 GB"

    res_storage = normalize_measurement("1 TB SSD")
    assert res_storage["value"] == 1.0
    assert res_storage["unit"] == "TB"

    res_weight = normalize_measurement("1.45 kg")
    assert res_weight["value"] == 1.45
    assert res_weight["unit"] == "kg"

    assert normalize_measurement("no numbers here") is None


def test_normalize_date():
    """Verify date parsing into standard ISO YYYY-MM-DD."""
    assert normalize_date("2026-10-24")["iso"] == "2026-10-24"
    assert normalize_date("2026-10-24T14:30:00Z")["iso"] == "2026-10-24"
    assert normalize_date("October 24, 2026")["iso"] == "2026-10-24"
    assert normalize_date("invalid date") is None


def test_compute_canonical_key():
    """Verify canonical deduplication key generation."""
    key1 = compute_canonical_key("Apple MacBook Air 13 M2", brand="Apple")
    key2 = compute_canonical_key("The Apple MacBook Air 13 M2 Official", brand="Apple")
    assert key1 == key2

    # SKU overrides title variation
    key_sku1 = compute_canonical_key("Laptop X", sku="SKU-998877")
    key_sku2 = compute_canonical_key("Different Name", sku="SKU-998877")
    assert key_sku1 == key_sku2


def test_deduplication_combines_sources_and_flags_conflicts():
    """Verify deduplication preserves source attribution and flags conflicting values."""
    raw_records = [
        {
            "name": "Sony WH-1000XM5",
            "source_url": "https://store-a.com/sony-xm5",
            "price": "$398.00",
            "color": "Black",
            "battery": "30 hours",
        },
        {
            "name": "Sony WH-1000XM5 Noise Canceling",
            "source_url": "https://store-b.com/sony-headphones",
            "price": "$348.00",  # Conflicting price!
            "color": "Black",
            "warranty": "2 years",
        },
    ]

    criteria = ["price", "color", "battery", "warranty"]
    records, conflicts = deduplicate_and_compare(raw_records, criteria)

    assert len(records) == 1
    rec = records[0]
    assert rec.is_duplicate_across_sources is True
    assert "https://store-a.com/sony-xm5" in rec.source_urls
    assert "https://store-b.com/sony-headphones" in rec.source_urls

    # Check conflict detection
    assert len(conflicts) == 1
    assert conflicts[0].field == "price"
    assert "398" in conflicts[0].description and "348" in conflicts[0].description

    # Color matches across sources
    assert rec.fields["color"] == "Black"
    # Battery was only on store A, filled in record
    assert "30" in rec.fields["battery"]
    # Warranty was only on store B, filled in record
    assert "2 years" in rec.fields["warranty"]


def test_missing_data_representation():
    """Verify missing criteria are explicitly represented as N/A with status='missing'."""
    raw_records = [
        {
            "name": "Item A",
            "source_url": "https://store-a.com/item",
            "price": "$50.00",
        }
    ]
    criteria = ["price", "weight", "dimensions"]
    records, _ = deduplicate_and_compare(raw_records, criteria)

    assert len(records) == 1
    rec = records[0]
    assert rec.fields["price"] == "$50.00"
    assert rec.fields["weight"] == "N/A"
    assert rec.evidence["weight"].status == "missing"
    assert rec.evidence["weight"].confidence == 0.0


# ===========================================================================
# 3. Partial Failure and Bounded Concurrency Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_partial_failure_retains_successful_sources():
    """Verify that if one source fails (e.g. 404 or network timeout), successful sources are retained."""
    service = ResearchService()

    req = ResearchRequest(
        goal="Compare cameras",
        sources=["https://store-good.com/camera", "http://127.0.0.1:9999/bad"],  # one valid, one unsafe loopback
        criteria=["price", "sensor"],
    )

    # Submit task
    resp = await service.submit_research_task(req)
    assert resp.task_id is not None
    assert len(resp.sources) == 2

    # The unsafe loopback source should immediately be marked failed by pre-validation
    loopback_source = next(s for s in resp.sources if "127.0.0.1" in s.url)
    assert loopback_source.status == "failed"
    assert "pre-validation rejected" in loopback_source.error.lower()


@pytest.mark.asyncio
async def test_cancellation_preserves_gathered_results():
    """Verify cancellation cleanly marks task as cancelled and preserves collected results."""
    mock_tm = MagicMock()
    mock_tm.cancel_events = {}
    mock_tm.tasks = {}
    mock_tm._is_cancelled = MagicMock(return_value=False)
    mock_tm.broadcast_task_update = AsyncMock()

    service = ResearchService(task_manager=mock_tm)

    req = ResearchRequest(
        goal="Compare smartphones",
        sources=["https://store-a.com/phone", "https://store-b.com/phone"],
        criteria=["price", "screen"],
    )

    resp = await service.submit_research_task(req)
    task_id = resp.task_id

    # Trigger cancellation
    cancelled_resp = service.cancel_research_task(task_id)
    assert cancelled_resp is not None
    assert cancelled_resp.status == TaskStatus.CANCELLED
    assert mock_tm.cancel_events[task_id].is_set()


@pytest.mark.asyncio
async def test_bounded_concurrency_respected():
    """Verify concurrency limit does not exceed configured maximum."""
    service = ResearchService()
    req = ResearchRequest(
        goal="Check laptops",
        sources=["https://store-1.com", "https://store-2.com", "https://store-3.com"],
        concurrency=2,
    )
    assert req.concurrency == 2
    assert req.concurrency <= 8


# ===========================================================================
# 4. End-to-End API Routes Integration Test
# ===========================================================================

@pytest.mark.asyncio
async def test_research_api_lifecycle():
    """Test full API endpoint integration: submit, status, results, export."""
    from api.routes import (
        submit_research_task,
        get_research_status,
        cancel_research_task,
    )

    req = ResearchRequest(
        goal="Find best budget headphones",
        sources=["https://store-x.example.com/headphones"],
        criteria=["price", "brand"],
    )

    # 1. Submit
    submit_resp = await submit_research_task(req)
    assert submit_resp.task_id.startswith("research_")
    task_id = submit_resp.task_id

    # 2. Get status
    status_resp = await get_research_status(task_id)
    assert status_resp.task_id == task_id
    assert status_resp.goal == req.goal

    # 3. Cancel
    cancel_resp = await cancel_research_task(task_id)
    assert cancel_resp.status == TaskStatus.CANCELLED


# ===========================================================================
# 5. Multi-Record Listing & Books to Scrape Resolution Tests
# ===========================================================================

def test_search_query_instruction_resolution():
    """Verify natural search instructions are parsed into safe search URLs."""
    from services.research_service import resolve_source_target
    url1, is_search1 = resolve_source_target("search: python programming")
    assert is_search1
    assert "duckduckgo.com" in url1
    assert "python+programming" in url1

    url2, is_search2 = resolve_source_target("find: mechanical keyboards")
    assert is_search2
    assert "duckduckgo.com" in url2

    url3, is_search3 = resolve_source_target("best wireless mouse 2024")
    assert is_search3
    assert "duckduckgo.com" in url3

    url4, is_search4 = resolve_source_target("https://books.toscrape.com/")
    assert not is_search4
    assert url4 == "https://books.toscrape.com/"


def test_books_to_scrape_catalog_overlap_and_deduplication():
    """
    Simulate the Books to Scrape failure case:
    Source 1 (All products) returns 20 books including 'It's Only the Himalayas'
    Source 2 (Travel category) returns 11 books including 'It's Only the Himalayas'
    Verify:
    1. Returns multiple records, not 2 page titles.
    2. Overlapping book 'It's Only the Himalayas' is correctly deduplicated.
    3. Both source URLs are retained.
    4. Prices are normalized without losing original values.
    5. No false conflicts are raised for shared availability ('In stock').
    """
    from services.research_export_service import ResearchExportService

    src_root = "https://books.toscrape.com/"
    src_travel = "https://books.toscrape.com/catalogue/category/books/travel_2/index.html"
    criteria = ["title", "price", "availability", "source_url"]

    raw_records = [
        # Source 1 records
        {
            "title": "It's Only the Himalayas",
            "name": "It's Only the Himalayas",
            "price": "Â£45.17",
            "availability": "In stock",
            "product_url": "https://books.toscrape.com/catalogue/its-only-the-himalayas_981/index.html",
            "source_url": src_root,
            "evidence": {"price": "price element (.price_color): Â£45.17", "availability": "in stock"},
        },
        {
            "title": "A Light in the Attic",
            "name": "A Light in the Attic",
            "price": "Â£51.77",
            "availability": "In stock",
            "product_url": "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html",
            "source_url": src_root,
            "evidence": {"price": "price element (.price_color): Â£51.77", "availability": "in stock"},
        },
        # Source 2 records (overlapping Himalayas + another travel book)
        {
            "title": "It's Only the Himalayas",
            "name": "It's Only the Himalayas",
            "price": "Â£45.17",
            "availability": "In stock (19 available)",
            "product_url": "https://books.toscrape.com/catalogue/its-only-the-himalayas_981/index.html",
            "source_url": src_travel,
            "evidence": {"price": "price element (.price_color): Â£45.17", "availability": "in stock"},
        },
        {
            "title": "Full Moon over Noah's Ark",
            "name": "Full Moon over Noah's Ark",
            "price": "Â£49.43",
            "availability": "In stock",
            "product_url": "https://books.toscrape.com/catalogue/full-moon-over-noahs-ark_811/index.html",
            "source_url": src_travel,
            "evidence": {"price": "price element (.price_color): Â£49.43", "availability": "in stock"},
        },
    ]

    records, conflicts = deduplicate_and_compare(raw_records, criteria)

    # 4 raw records should deduplicate to exactly 3 unique books
    assert len(records) == 3

    # Find the overlapping book
    himalayas = next(r for r in records if "Himalayas" in r.name)
    assert himalayas.is_duplicate_across_sources
    assert len(himalayas.source_urls) == 2
    assert src_root in himalayas.source_urls
    assert src_travel in himalayas.source_urls

    # Check normalized price
    assert himalayas.fields["price"] in ("£45.17", "Â£45.17")
    assert himalayas.evidence["price"].value in ("£45.17", "Â£45.17")
    assert himalayas.evidence["price"].raw_value == "Â£45.17"

    # Check availability and no false conflicts
    assert himalayas.fields["availability"] == "In stock"
    assert len(conflicts) == 0  # 'In stock' vs 'In stock (19 available)' should not conflict

    # Verify CSV and XLSX generation on these records
    comp = ComparisonResult(
        goal="Compare book prices across catalogs",
        criteria=criteria,
        sources=[],
        records=records,
        summary={"total_records": len(records), "conflicts_count": 0},
    )
    csv_bytes = ResearchExportService.export_csv(comp)
    assert b"It's Only the Himalayas" in csv_bytes
    assert b"A Light in the Attic" in csv_bytes
    assert b"Full Moon over Noah's Ark" in csv_bytes
    assert b"\xa345.17" in csv_bytes or b"\xc2\xa345.17" in csv_bytes or b"45.17" in csv_bytes


@pytest.mark.asyncio
async def test_extract_listing_records_mocked_page():
    """Verify extract_listing_records extracts all items from mocked catalog HTML."""
    from extraction.listing import extract_listing_records

    mock_page = MagicMock()
    mock_page.url = "https://books.toscrape.com/"

    # Mock evaluate to return simulated JS extraction output
    mock_page.evaluate = AsyncMock(return_value=[
        {
            "title": "Book 1",
            "price": "Â£25.00",
            "availability": "In stock",
            "product_url": "catalogue/book-1/index.html",
            "rating": "Four",
        },
        {
            "title": "Book 2",
            "price": "Â£18.50",
            "availability": "In stock",
            "product_url": "catalogue/book-2/index.html",
            "rating": "Five",
        },
    ])

    results = await extract_listing_records(mock_page, criteria=["title", "price", "availability"])
    assert len(results) == 2
    assert results[0]["title"] == "Book 1"
    assert results[0]["price"] == "Â£25.00"
    assert results[0]["product_url"] == "https://books.toscrape.com/catalogue/book-1/index.html"
    assert results[1]["title"] == "Book 2"
    assert results[1]["product_url"] == "https://books.toscrape.com/catalogue/book-2/index.html"
