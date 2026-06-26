from scripts.scrape_cola_dataset import ColaRow, parse_listing, parse_printable


def test_parse_listing_deduplicates_ttb_ids():
    html = """
    <tr><td><a href="viewColaDetails.do?ttbid=12345678901234">one</a></td><td>06/01/2026</td></tr>
    <tr><td><a href="viewColaDetails.do?ttbid=12345678901234">one again</a></td><td>06/01/2026</td></tr>
    <tr><td><a href="viewColaDetails.do?ttbid=99999999999999">two</a></td><td>06/02/2026</td></tr>
    """
    assert parse_listing(html) == [
        ("12345678901234", "06/01/2026"),
        ("99999999999999", "06/02/2026"),
    ]


def test_parse_printable_extracts_manifest_fields():
    page = """
    <div class="label">BRAND NAME</div><div class="data">North Orchard</div>
    <div class="label">NET CONTENTS</div><div class="data">355 mL</div>
    <div class="label">ALCOHOL CONTENT</div><div class="data">6.2%</div>
    <div class="label">CLASS/TYPE DESCRIPTION</div><div class="data">Hard Cider</div>
    <input alt="Type of Product: Wine">
    <input alt="Type of Product: Malt Beverage" checked>
    """
    row = parse_printable(page, "12345678901234", "06/01/2026")
    product = row.manifest_product()
    assert isinstance(row, ColaRow)
    assert product["product_id"] == "cola-12345678901234"
    assert product["brand_name"] == "North Orchard"
    assert product["class_type"] == "Hard Cider"
    assert product["alcohol_content"] == "6.2%"
    assert product["net_contents"] == "355 mL"
