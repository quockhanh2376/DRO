from __future__ import annotations

import pytest
import re
import time
from pathlib import Path
from fastapi.testclient import TestClient

from app.api.application import create_app
from app.core import rewrites
from app.db.database import Database
from app.db.models import (AuditLogRecord, Base, BenchmarkResultRecord, BenchmarkRunRecord,
                           BenchmarkSampleRecord, RewriteHistoryRecord, TargetRecord)
from app.db.repositories import (add_rewrite_history, get_setting, save_benchmark_run,
                                 save_optimizer_state, save_target)
from app.models.benchmark import BenchmarkResult, DecisionResult, PendingCandidateState
from app.models.target import Target
from app.version import VERSION_DISPLAY
from sqlalchemy import select


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("DRO_ADMIN_USER", "admin")
    monkeypatch.setenv("DRO_ADMIN_PASSWORD", "test-admin-password")
    monkeypatch.setenv("ADGUARD_URL", "http://admin:ui-secret@adguard.example")
    database = Database(f"sqlite:///{(tmp_path / 'web.db').as_posix()}")
    Base.metadata.create_all(database.engine)
    app = create_app(database=database, benchmark_cycle=lambda *_args: {})
    with TestClient(app) as client:
        login_page = client.get("/login")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
        assert client.post("/login", data={"username": "admin", "password": "test-admin-password",
                                           "csrf_token": csrf}, follow_redirects=False).status_code == 303
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/targets").text).group(1)
        client.headers["x-csrf-token"] = csrf
        yield client, database
    database.close()


def test_global_header_uses_central_version_on_authenticated_pages(web, monkeypatch):
    import app.web.routes as web_routes

    client, database = web
    assert VERSION_DISPLAY == "v1.0.8"
    monkeypatch.setattr(web_routes, "VERSION_DISPLAY", "v9.8.7-test")
    with database.session() as session:
        target = save_target(session, Target(hostname="version.example"))

    for path in ("/", "/targets", f"/targets/{target.id}", "/history"):
        response = client.get(path)
        assert response.status_code == 200
        assert '<a class="brand" href="/">DRO <span class="brand-version">v9.8.7-test</span></a>' in response.text
        assert response.text.count("v9.8.7-test") == 1
        assert 'class="app-version"' not in response.text


def test_runtime_version_is_only_shown_in_global_header(web):
    client, database = web
    with database.session() as session:
        target = save_target(session, Target(hostname="version-once.example"))

    for path in ("/", "/targets", f"/targets/{target.id}", "/history"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.text.count(VERSION_DISPLAY) == 1
        assert response.text.count('class="brand-version"') == 1

    login = client.get("/login")
    assert VERSION_DISPLAY not in login.text


def test_global_header_version_is_small_and_regular_weight():
    stylesheet = (Path(__file__).parents[1] / "app/web/static/style.css").read_text()
    rules = re.search(r"\.brand\s+\.brand-version\{([^}]*)\}", stylesheet)
    assert rules
    assert "font-size:16px" in rules.group(1)
    assert "font-weight:400" in rules.group(1)


def test_dashboard_and_targets_render(web):
    client, _database = web
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert client.app.version == "1.0.8" and "v1.0.8" in dashboard.text
    assert "Dashboard" in dashboard.text and "Runtime Settings" in dashboard.text
    assert dashboard.text.index("</table>") < dashboard.text.index('id="settings"')
    assert all(f'name="{name}"' in dashboard.text for name in (
        "value", "unit", "enabled", "max_auto_changes_per_day", "days", "csrf_token"))
    assert 'name="value" min="1" step="1"' in dashboard.text
    assert "Benchmark history retention" in dashboard.text
    assert "AdGuard URL" in dashboard.text and "Save interval" in dashboard.text
    master_form = dashboard.text.split('action="/settings/automatic-rewrite"', 1)[1].split("</form>", 1)[0]
    assert "Automatic DNS Rewrite" in dashboard.text and 'name="enabled" value="true"' in master_form
    assert "Only targets with Auto Apply enabled will be allowed to change DNS automatically." in dashboard.text
    nav = dashboard.text.split("<nav>", 1)[1].split("</nav>", 1)[0]
    assert all(f'href="{path}"' in nav for path in ("/", "/targets", "/history"))
    assert 'href="/settings"' not in nav and ">Settings</a>" not in nav
    legacy = client.get("/settings", follow_redirects=False)
    assert legacy.status_code == 303 and legacy.headers["location"] == "/#settings"
    response = client.get("/targets")
    assert response.status_code == 200 and "Add target" in response.text
    with _database.session() as session:
        save_target(session, Target(hostname="listed.example"))
    targets_page = client.get("/targets").text
    assert 'title="Run benchmark now"' in targets_page
    assert 'hx-target="#run-state-poll-1"' in targets_page and 'hx-swap="innerHTML"' in targets_page
    assert "Running benchmark" not in targets_page
    assert 'class="activity-spinner benchmark-spinner htmx-indicator"' not in targets_page
    assert 'hx-get="/targets/' in targets_page and 'run/state' in targets_page
    assert 'id="run-state-poll-' in targets_page
    assert 'id="benchmark-indicator-' not in targets_page
    assert 'id="run-result-1"' in targets_page
    assert targets_page.index('id="run-result-1"') < targets_page.index("</table>")


def test_targets_form_is_compact_and_actions_are_play_edit_delete(web):
    client, database = web
    with database.session() as session:
        target = save_target(session, Target(hostname="actions.example"))
        target_id = target.id
        assert target.auto_apply is False
    page = client.get("/targets").text
    header = page.split("<thead>", 1)[1].split("</thead>", 1)[0]
    assert "Auto Apply" in header and "Mode" not in header
    assert 'class="target-form-primary"' in page and 'name="hostname"' in page
    assert 'class="check"><input type="checkbox" name="enabled"' in page
    options = page.split('class="target-form-options">', 1)[1].split("</div>", 1)[0]
    for field in ("interval_hours", "mode", "runs_per_ip", "switch_threshold_ms",
                  "switch_threshold_percent", "manual_lock_ip"):
        assert f'name="{field}"' in options
    css = client.get("/static/style.css").text
    assert ".target-form-options{display:grid;grid-template-columns:repeat(6,minmax(0,1fr))" in css
    assert "@media(max-width:900px){.target-form-options{grid-template-columns:repeat(3,minmax(0,1fr))}}" in css

    row = page.split('class="actions">', 1)[1].split("</td>", 1)[0]
    play = row.index('title="Run benchmark now"')
    edit = row.index(f'href="/targets/{target_id}/edit"')
    delete = row.index('class="link danger"')
    ping = row.index(f'action="/targets/{target_id}/ping"')
    assert play < edit < delete < ping
    assert '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>' in row
    assert f'hx-post="/targets/{target_id}/run"' in row
    assert f'action="/targets/{target_id}/run"' in row
    assert '<button class="button" type="submit">Run Now</button>' not in row
    assert f'data-copy="actions.example"' in page and 'title="Copy hostname"' in page
    assert '<script src="/static/targets.js" defer></script>' in page
    assert 'navigator.clipboard.writeText(value)' in client.get("/static/targets.js").text


def test_targets_are_alphabetical_numbered_and_search_controls_are_wired(web):
    client, database = web
    with database.session() as session:
        z_target = save_target(session, Target(hostname="zulu.example"))
        m_target = save_target(session, Target(hostname="Myob.com"))
        a_target = save_target(session, Target(hostname="accounts.intuit.com"))
        ids = {"zulu.example": z_target.id, "myob.com": m_target.id,
               "accounts.intuit.com": a_target.id}
    page = client.get("/targets").text
    blocks = re.findall(r'<tbody class="target-block" data-target-id="(\d+)" data-hostname="([^"]+)"', page)
    assert [hostname for _target_id, hostname in blocks] == [
        "accounts.intuit.com", "myob.com", "zulu.example"]
    assert [int(re.search(r'<td class="target-sequence">(\d+)</td>',
                          page.split(f'data-target-id="{target_id}"', 1)[1].split("</tbody>", 1)[0]).group(1))
            for target_id, _hostname in blocks] == [1, 2, 3]
    for hostname, target_id in ids.items():
        assert f'id="target-block-{target_id}"' in page
        assert f'data-hostname="{hostname}"' in page
    card = page.split('class="panel"', 1)[1].split("</section>", 1)[0]
    assert card.index('class="target-search"') < card.index('class="target-form"')
    assert 'id="target-search-input"' in card and 'type="search"' in card
    assert card.count('class="target-search-clear"') == 1
    assert 'id="target-search-clear"' in card and 'title="Clear search"' in card
    search_field = card.split('class="target-search-field"', 1)[1].split("</div>", 1)[0]
    assert 'class="target-search-input"' in search_field
    assert 'class="target-search-clear"' in search_field
    assert '>Search</button>' not in page
    for target_id, _hostname in blocks:
        tbody = page.split(f'data-target-id="{target_id}"', 1)[1].split("</tbody>", 1)[0]
        assert f'id="target-block-{target_id}"' in tbody
        assert f'id="ping-result-{target_id}"' in tbody
        assert f'id="run-result-{target_id}"' in tbody
        assert f'hx-target="#run-state-poll-{target_id}"' in tbody
    script = client.get("/static/targets.js").text
    assert 'document.addEventListener("input"' in script
    assert 'event.target.matches("#target-search-input")' in script
    assert 'document.addEventListener("click"' in script
    assert 'input.value = ""' in script and 'input.focus()' in script
    assert "reorderTargetBlocks(event.target)" in script and "reorderTargetBlocks(input)" in script
    assert 'name === query ? 0 : name.startsWith(query) ? 1 : name.includes(query) ? 2 : 3' in script
    assert 'targetSearchCollator.compare(a.dataset.hostname, b.dataset.hostname)' in script
    assert 'document.addEventListener("click"' in script
    assert 'input.focus()' in script and 'input.value = ""' in script
    assert 'table.appendChild(block)' in script and 'target-sequence' in script
    assert 'querySelectorAll("tbody.target-block[data-hostname]")' in script
    assert 'blocks.forEach((block) => {' in script
    assert 'block.classList.remove("search-match", "search-best-match", "search-exact-match")' in script
    assert 'block.classList.add("search-match")' in script
    assert 'block.classList.add("search-best-match")' in script
    assert 'block.classList.add("search-exact-match")' in script
    assert 'if (!query) return' in script
    stylesheet = client.get("/static/style.css").text
    search_input_style = stylesheet.split(".target-search-input{", 1)[1].split("}", 1)[0]
    assert "padding:8px 43px 8px 11px" in search_input_style
    assert "font:400 17px/1.35" in search_input_style
    assert ".target-search-input::placeholder{color:var(--muted);font:inherit" in stylesheet
    assert ".target-search-clear{position:absolute" in stylesheet
    assert ".target-search-field{position:relative" in stylesheet
    assert ".target-block.search-match .benchmark-hostname" in stylesheet
    assert ".target-block.search-best-match .benchmark-hostname" in stylesheet
    assert ".target-block.search-exact-match .benchmark-hostname" in stylesheet
    search_styles = stylesheet.split(".target-block.search-match", 1)[1]
    assert "animation:" not in search_styles.split("@media", 1)[0]


def test_live_target_search_ranking_contract_for_xero_and_clear(web):
    client, database = web
    with database.session() as session:
        for hostname in ("zeta.example", "reporting.xero.com", "go.xero.com",
                         "app.practicemanager.xero.com", "xero.example", "accounts.intuit.com",
                         "alpha.xero.net", "xero-tools.example"):
            save_target(session, Target(hostname=hostname))
    script = client.get("/static/targets.js").text
    rank_start = script.index("const rank = (name)")
    rank_end = script.index("const ordered =", rank_start)
    rank_expression = script[rank_start:rank_end]
    assert rank_expression.index("name === query") < rank_expression.index("name.startsWith(query)")
    assert rank_expression.index("name.startsWith(query)") < rank_expression.index("name.includes(query)")
    assert "rank(left) - rank(right) || targetSearchCollator.compare" in script
    assert 'const query = input.value.trim().toLocaleLowerCase()' in script
    assert 'querySelectorAll("tbody.target-block[data-hostname]")' in script
    assert "targetSearchOriginalOrder" in script and 'input.value = ""' in script
    # Whole target blocks retain ping and benchmark result rows during reordering.
    page = client.get("/targets").text
    assert 'class="target-block" data-target-id=' in page
    blocks = re.findall(r'<tbody class="target-block" data-target-id="\d+" data-hostname="([^"]+)"', page)
    assert blocks == ["accounts.intuit.com", "alpha.xero.net", "app.practicemanager.xero.com",
                      "go.xero.com", "reporting.xero.com", "xero-tools.example", "xero.example", "zeta.example"]
    body_template = page.split('class="target-block"', 1)[1].split("</tbody>", 1)[0]
    assert 'class="ping-row"' in body_template and 'class="run-result-row"' in body_template
    assert "rank(left) - rank(right)" in script
    # Query "xero": prefix first, then substring matches A-Z, then non-matches A-Z.
    expected = ["xero-tools.example", "alpha.xero.net", "app.practicemanager.xero.com",
                "go.xero.com", "reporting.xero.com", "xero.example",
                "accounts.intuit.com", "zeta.example"]
    assert expected[:6] == ["xero-tools.example", "alpha.xero.net",
                            "app.practicemanager.xero.com", "go.xero.com",
                            "reporting.xero.com", "xero.example"]
    assert "index + 1" in script and 'sequence.textContent = String(index + 1)' in script


def test_live_target_search_highlights_all_matches_and_clears_classes(web):
    client, _database = web
    script = client.get("/static/targets.js").text
    apply_start = script.index('const matches = ordered.filter')
    apply_end = script.index("// Delegation keeps search alive", apply_start)
    feedback = script[apply_start:apply_end]
    assert 'matches.forEach((block, index)' in feedback
    assert 'block.classList.add("search-match")' in feedback
    assert 'if (index === 0) block.classList.add("search-best-match")' in feedback
    assert 'block.classList.add("search-exact-match")' in feedback
    assert 'classList.remove("search-match", "search-best-match", "search-exact-match")' in script
    css = client.get("/static/style.css").text
    assert ".target-block.search-match .benchmark-hostname" in css
    assert ".target-block.search-best-match .benchmark-hostname" in css
    assert ".target-block.search-exact-match .benchmark-hostname" in css
    assert "outline:2px solid #83d99a" in css


def test_target_form_rejects_url_syntax_and_normalizes_hostname(web):
    client, database = web
    rejected = client.post("/targets", data={"hostname": "https://example.com"})
    assert rejected.status_code == 422
    assert "valid DNS labels" in rejected.text
    saved = client.post("/targets", data={"hostname": "  Mixed.Example.COM  "}, follow_redirects=False)
    assert saved.status_code == 303
    with database.session() as session:
        assert session.scalar(select(TargetRecord.hostname)) == "mixed.example.com"


def test_unresolvable_valid_hostname_renders_diagnostic_state(web):
    client, database = web
    hostname = "go.fyi.appabc"
    with database.session() as session:
        target = save_target(session, Target(hostname=hostname, mode="auto", auto_apply=True))
        save_benchmark_run(session, target.id, [], {
            "resolution_failed": True,
            "discovery_error": "No valid public IPv4 A records were returned.",
            "current_rewrite_ip": None,
            "current_rewrite_lookup_succeeded": False,
            "public_ips": [], "candidate_ips": [],
        }, DecisionResult(action="RESOLUTION_FAILED",
                          reason="Public DNS discovery failed or returned no valid IP."))
        target_id = target.id
    page = client.get(f"/targets/{target_id}").text
    assert 'class="unavailable-value">Unavailable</span>' in page
    assert page.count('class="unavailable-value">Unavailable</span>') == 2
    assert "No valid IP found" in page
    assert "Unable to resolve this domain. Please double-check the hostname and try again." in page
    assert "RESOLUTION FAILED" in page
    assert "Public DNS discovery failed or returned no valid IP." in page
    assert "No benchmark results" in page
    assert "Apply Best IP" not in page and 'action="/targets/' + str(target_id) + '/apply-best"' not in page
    assert "Auto Apply" not in page
    css = client.get("/static/style.css").text
    assert ".unavailable-value{color:var(--danger);font-weight:700}" in css
    assert ".resolution-warning{" in css and ".resolution-failed-badge{" in css


def test_unhealthy_current_without_healthy_alternative_stays_critical_and_no_apply(web, monkeypatch):
    client, database = web
    import app.web.routes as web_routes
    hostname = "www.ato.gov.au"
    with database.session() as session:
        target = save_target(session, Target(hostname=hostname, mode="auto", auto_apply=True))
        failed = BenchmarkResult(ip="113.171.12.192", healthy=False, valid_runs=0, requested_runs=10,
                                 health_reason="TLS verification failed")
        current = BenchmarkResult(ip="113.171.12.178", healthy=False, valid_runs=0, requested_runs=10,
                                  health_reason="HTTP 403 outside accepted range 200-399")
        save_benchmark_run(session, target.id, [failed, current], {
            "resolution_failed": False, "current_rewrite_ip": current.ip,
            "current_rewrite_lookup_succeeded": True,
            "public_ips": [failed.ip], "candidate_ips": [failed.ip, current.ip],
            "candidate_health_reasons": {failed.ip: "10x TLS certificate verification failure",
                                         current.ip: "10x HTTP 403"},
            "failure_summary": "10x TLS certificate verification failure; 10x HTTP 403",
        }, DecisionResult(action="KEEP", current_ip=current.ip,
                          reason="Current IP is unhealthy and no healthy alternative is available."))
        save_optimizer_state(session, target.id, current.ip, PendingCandidateState(),
                             DecisionResult(action="KEEP", current_ip=current.ip,
                                            reason="Current IP is unhealthy and no healthy alternative is available."))
        target_id = target.id
    monkeypatch.setattr(web_routes, "configured_adguard_client", lambda: type("Client", (), {
        "get_rewrite": lambda _self, _host: {"domain": hostname, "answer": current.ip},
        "close": lambda _self: None,
    })())
    detail = client.get(f"/targets/{target_id}").text
    dashboard = client.get("/").text
    assert "Current IP is unhealthy and no healthy alternative is available." in detail
    assert "113.171.12.192" in detail and "10x TLS certificate verification failure" in detail
    assert "10x HTTP 403" in detail
    assert "No healthy candidate: 10x TLS certificate verification failure; 10x HTTP 403." in detail
    assert ">Critical<" in dashboard
    assert "Best IP" in detail and "Apply Best IP" not in detail
    assert f'action="/targets/{target_id}/apply-best"' not in detail
    assert "Auto Apply" not in detail


def test_global_and_target_auto_apply_controls_require_both_opt_ins(web):
    client, database = web
    settings = client.get("/").text
    assert 'class="status disabled">Disabled</strong>' in settings
    rejected = client.post("/settings/automatic-rewrite", data={"enabled": "true"})
    assert rejected.status_code == 400
    with database.session() as session:
        assert get_setting(session, "global_auto_master") is None

    enabled = client.post("/settings/automatic-rewrite", data={"enabled": "true", "confirm": "true"})
    assert enabled.status_code == 200 and enabled.url.path == "/"
    with database.session() as session:
        assert get_setting(session, "global_auto_master") == "true"
        target = save_target(session, Target(hostname="auto-toggle.example"))
        target_id = target.id
        assert target.auto_apply is False

    page = client.get("/targets").text
    assert f'action="/targets/{target_id}/auto-apply"' in page
    assert f'title="Enable Auto Apply"' in page and 'class="button auto-apply-toggle off"' in page
    toggled = client.post(f"/targets/{target_id}/auto-apply", data={"enabled": "true"})
    assert toggled.status_code == 200
    with database.session() as session:
        assert session.get(TargetRecord, target_id).auto_apply is True


def test_target_summary_and_rewrite_controls_share_compact_card(web):
    client, database = web
    with database.session() as session:
        target = save_target(session, Target(hostname="cards.example"))
        target_id = target.id
    page = client.get(f"/targets/{target_id}")
    assert page.text.count('class="panel target-detail-panel"') == 1
    panel = page.text.split('class="panel target-detail-panel"', 1)[1].split("</section>", 1)[0]
    assert "Rewrite controls" not in page.text
    assert panel.index("Current rewrite") < panel.index("Best IP") < panel.index("Pending winner") < panel.index("Mode / status")
    assert "Manual lock:" in panel and 'placeholder="Optional IPv4"' in panel
    assert ">Lock IP</button>" in panel
    assert 'action="/targets/' + str(target_id) + '/rollback"' not in panel
    assert "Run Now" in page.text
    css = client.get("/static/style.css").text
    assert ".target-detail-overview{display:grid;grid-template-columns:repeat(4,minmax(0,1fr))" in css
    assert "@media(max-width:1000px){.target-detail-overview{grid-template-columns:repeat(2,minmax(0,1fr))}}" in css
    assert "@media(max-width:520px){.target-detail-overview{grid-template-columns:1fr}" in css
    assert ".target-detail-panel{padding:15px 18px;margin:12px 0}" in css
    assert ".target-detail-controls{display:flex;align-items:center;flex-wrap:wrap" in css


def test_target_detail_locked_controls_keep_unlock_action(web):
    client, database = web
    with database.session() as session:
        target = save_target(session, Target(hostname="locked-detail.example", manual_lock_ip="192.0.2.55"))
        target_id = target.id
    page = client.get(f"/targets/{target_id}").text
    panel = page.split('class="panel target-detail-panel"', 1)[1].split("</section>", 1)[0]
    assert 'data-copy="192.0.2.55"' in panel
    assert f'action="/targets/{target_id}/unlock"' in panel and "Unlock IP" in panel
    assert 'placeholder="Optional IPv4"' not in panel
    unlocked = client.post(f"/targets/{target_id}/unlock", follow_redirects=False)
    assert unlocked.status_code == 303
    assert 'placeholder="Optional IPv4"' in client.get(f"/targets/{target_id}").text


def test_benchmark_summary_wraps_long_decision_and_timestamp(web):
    client, database = web
    long_reason = "decision-reason-" * 40
    with database.session() as session:
        target = save_target(session, Target(hostname="wrap.example"))
        save_benchmark_run(session, target.id, [], {}, DecisionResult(action="UPDATE", reason=long_reason))
        target_id = target.id
    page = client.get(f"/targets/{target_id}").text
    css = client.get("/static/style.css").text
    assert long_reason in page and 'class="decision-reason"' in page
    assert 'class="run-timestamp"' in page
    assert ".benchmark-decision-panel{display:grid;align-content:center;gap:16px}" in css
    assert ".benchmark-decision .decision-reason,.benchmark-timestamp .run-timestamp{min-width:0;overflow-wrap:anywhere;word-break:break-word}" in css
    assert 'class="benchmark-result-details"' in page
    assert 'class="benchmark-decision-panel"' in page


def test_benchmark_result_uses_requested_two_row_structure(web):
    client, database = web
    with database.session() as session:
        target = save_target(session, Target(hostname="layout.example"))
        best = BenchmarkResult(ip="192.0.2.30", valid_runs=8, requested_runs=10, healthy=True,
                              average_ms=12, median_ms=11, min_ms=10, max_ms=15, jitter_ms=2)
        save_benchmark_run(session, target.id, [best], {
            "current_rewrite_ip": "192.0.2.31", "candidate_ips": ["192.0.2.30", "192.0.2.31"]
        }, DecisionResult(action="HOLD", reason="Candidate is faster"))
        target_id = target.id
    page = client.get(f"/targets/{target_id}").text
    top_row = page.split('class="benchmark-result-top"', 1)[1].split('class="benchmark-result-details"', 1)[0]
    second_row = page.split('class="benchmark-result-details"', 1)[1].split('class="table-wrap"', 1)[0]
    assert 'Benchmark result: <span class="benchmark-hostname">layout.example</span>' in top_row
    assert "Current IP" in top_row and "Best IP" in top_row
    assert top_row.index("Current IP") < top_row.index("Best IP")
    assert "Apply Best IP" not in top_row
    assert "Discovered candidates" in second_row and "192.0.2.30" in second_row
    assert "Decision:" in second_row and "Candidate is faster" in second_row
    assert "Run Timestamp:" in second_row
    assert second_row.count('class="candidate-list"') == 1
    css = client.get("/static/style.css").text
    assert ".benchmark-result-details{grid-template-columns:minmax(0,1fr) minmax(0,3fr)}" in css
    assert ".candidate-list{display:grid;gap:7px;min-width:0}" in css
    assert ".benchmark-decision-panel{display:grid;align-content:center;gap:16px}" in css
    assert ".benchmark-decision .decision-reason,.benchmark-timestamp .run-timestamp{min-width:0;overflow-wrap:anywhere;word-break:break-word}" in css
    assert ".ping-live pre{font-size:14px;line-height:1.6}" in css


def test_benchmark_result_hostname_has_readable_highlight_and_wraps(web):
    client, database = web
    hostname = "very-long-customer-subdomain.example.internal"
    with database.session() as session:
        target = save_target(session, Target(hostname=hostname))
        save_benchmark_run(session, target.id, [], {}, DecisionResult(action="KEEP", reason="Current IP remains best"))
        target_id = target.id
    page = client.get(f"/targets/{target_id}").text
    css = client.get("/static/style.css").text
    assert f'Benchmark result: <span class="benchmark-hostname">{hostname}</span>' in page
    assert ".benchmark-hostname{display:inline-flex;max-width:100%" in css
    assert "color:#9ae6bd" in css and "overflow-wrap:anywhere;word-break:break-word" in css


def test_live_ping_start_selects_best_and_renders_console_with_stop_control(web, monkeypatch):
    client, database = web
    import app.web.routes as web_routes
    from datetime import datetime, timezone
    from threading import Lock
    from types import SimpleNamespace

    with database.session() as session:
        target = save_target(session, Target(hostname="ping.example"))
        current = BenchmarkResult(ip="192.0.2.10", healthy=True, valid_runs=10, requested_runs=10,
                                  average_ms=20, median_ms=20, min_ms=18, max_ms=22, jitter_ms=2)
        best = BenchmarkResult(ip="192.0.2.11", healthy=True, valid_runs=10, requested_runs=10,
                               average_ms=10, median_ms=10, min_ms=9, max_ms=11, jitter_ms=1)
        save_benchmark_run(session, target.id, [current, best],
                           {"current_rewrite_ip": current.ip}, DecisionResult(action="KEEP", reason="test"))
        target_id = target.id
    page = client.get("/targets")
    assert f'action="/targets/{target_id}/ping"' in page.text
    assert f'hx-target="#ping-result-{target_id}"' in page.text
    assert f'hx-get="/targets/{target_id}/ping/output"' in page.text
    assert 'hx-trigger="load, every 1s"' in page.text
    calls = []

    class FakePings:
        def start(self, identifier, ip):
            calls.append((identifier, ip))
            return SimpleNamespace(target_id=identifier, ip=ip, lines=["64 bytes from 192.0.2.11: time=4 ms"],
                                   lock=Lock(), started_at=datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc))

    monkeypatch.setattr(web_routes, "ping_sessions", FakePings())
    response = client.post(f"/targets/{target_id}/ping", headers={"HX-Request": "true"})
    assert response.status_code == 200 and calls == [(target_id, "192.0.2.11")]
    assert f'hx-get="/targets/{target_id}/ping/output"' in page.text
    assert "Live ping" in response.text and "64 bytes from 192.0.2.11" in response.text
    assert "27-09-2026 10:00:00 VNTime" in response.text
    assert f'/targets/{target_id}/ping/stop' in response.text
    assert 'aria-label="Stop ping"' in response.text
    assert "ping-live-spinner" in response.text
    assert 'hx-swap-oob="innerHTML:#ping-status-' in response.text


def test_live_ping_stop_requires_csrf_and_clears_active_ui(web, monkeypatch):
    client, database = web
    import app.web.routes as web_routes
    from datetime import datetime, timezone
    from threading import Lock
    from types import SimpleNamespace

    with database.session() as session:
        fallback = save_target(session, Target(hostname="ping-fallback.example"))
        save_benchmark_run(session, fallback.id, [BenchmarkResult(
            ip="192.0.2.20", healthy=False, valid_runs=0, requested_runs=10)],
            {"current_rewrite_ip": "192.0.2.21"}, DecisionResult(action="KEEP", reason="test"))
        target_id = fallback.id

    class FakePings:
        def __init__(self):
            self.session = SimpleNamespace(target_id=target_id, ip="192.0.2.21",
                lines=["64 bytes from 192.0.2.21: time=8 ms"], lock=Lock(),
                started_at=datetime.now(timezone.utc), process=SimpleNamespace(poll=lambda: None))
            self.stopped = False
        def start(self, identifier, ip):
            assert (identifier, ip) == (target_id, "192.0.2.21")
            return self.session
        def get(self, identifier):
            return self.session if identifier == target_id else None
        def is_active(self, identifier):
            return identifier == target_id and not self.stopped
        def stop(self, identifier):
            self.stopped = True
            return self.session

    fake = FakePings()
    monkeypatch.setattr(web_routes, "ping_sessions", fake)
    start = client.post(f"/targets/{target_id}/ping")
    assert "192.0.2.21" in start.text
    output = client.get(f"/targets/{target_id}/ping/output")
    assert "64 bytes from 192.0.2.21" in output.text and "hx-get" not in output.text
    assert client.post(f"/targets/{target_id}/ping/stop", data={},
                       headers={"x-csrf-token": "invalid"}).status_code == 403
    stopped = client.post(f"/targets/{target_id}/ping/stop")
    assert stopped.status_code == 200 and fake.stopped
    assert "Ping stopped." in stopped.text
    assert f'hx-get="/targets/{target_id}/ping/output"' not in stopped.text
    assert f'hx-swap-oob="innerHTML:#ping-status-{target_id}"' in stopped.text


def test_ping_timeout_state_shows_auto_stop_and_removes_stop_control(web, monkeypatch):
    client, database = web
    import app.web.routes as web_routes
    from datetime import datetime, timezone
    from threading import Lock
    from types import SimpleNamespace
    with database.session() as session:
        target = save_target(session, Target(hostname="ping-timeout.example"))
        target_id = target.id

    session = SimpleNamespace(target_id=target_id, ip="192.0.2.51", lines=[], lock=Lock(),
                              started_at=datetime.now(timezone.utc), stop_reason="timeout",
                              process=SimpleNamespace(poll=lambda: 0))

    class FinishedPing:
        def get(self, identifier): return session if identifier == target_id else None
        def is_active(self, _identifier): return False

    monkeypatch.setattr(web_routes, "ping_sessions", FinishedPing())
    response = client.get(f"/targets/{target_id}/ping/output")
    assert "Stopped automatically after 60 seconds" in response.text
    assert "stop-ping" not in response.text and "ping-live-spinner" not in response.text


def test_live_ping_without_known_ip_shows_message_and_no_stop_control(web, monkeypatch):
    client, database = web
    import app.web.routes as web_routes
    with database.session() as session:
        target = save_target(session, Target(hostname="ping-empty.example"))
        target_id = target.id
    monkeypatch.setattr(web_routes.ping_sessions, "start",
                        lambda *_args: pytest.fail("must not start a ping without known IP"))
    response = client.post(f"/targets/{target_id}/ping")
    assert "No IP available" in response.text
    assert "/ping/stop" not in response.text


def test_benchmark_state_indicator_tracks_coordinator_and_reenables_play(web):
    client, database = web
    with database.session() as session:
        target = save_target(session, Target(hostname="run-state.example"))
        target_id = target.id
    coordinator = client.app.state.run_coordinator
    with coordinator.run(target_id):
        page = client.get("/targets").text
        assert f'id="play-button-{target_id}"' in page
        play = page.split(f'id="play-button-{target_id}"', 1)[1].split(">", 1)[0]
        assert "disabled" in play and "aria-busy=\"true\"" in play
        assert f'id="run-state-indicator-{target_id}"' in page
        state = client.get(f"/targets/{target_id}/run/state")
        assert f'id="run-state-indicator-{target_id}"' in state.text
        assert f'hx-swap-oob="outerHTML:#play-button-{target_id}"' in state.text
        assert "ping-live-spinner" not in state.text and "/ping/stop" not in state.text
    finished = client.get(f"/targets/{target_id}/run/state")
    assert f'id="run-state-indicator-{target_id}"' not in finished.text
    assert 'title="Run benchmark now"' in finished.text
    assert f'hx-swap-oob="outerHTML:#play-button-{target_id}"' in finished.text
    assert "disabled" not in finished.text.split('id="play-button-', 1)[1].split(">", 1)[0]


def test_login_layout_is_compact_and_responsive(web):
    client, _database = web
    page = client.get("/login").text
    assert '<main class="login">' in page
    assert 'name="username"' in page and 'name="password"' in page
    assert 'class="button primary span-2"' in page
    css = client.get("/static/style.css").text
    assert ".login{max-width:720px;width:52vw;margin:0 auto}" in css
    assert ".login .form-grid{grid-template-columns:repeat(2,minmax(0,1fr))}" in css
    assert "@media(max-width:760px){.login{max-width:100%;width:100%}.login .form-grid{grid-template-columns:1fr}}" in css


def test_create_edit_target_forms_and_validation(web):
    client, _database = web
    created = client.post("/targets", data={"hostname": "Example.com", "enabled": "true",
                                             "mode": "recommend", "interval_hours": "4",
                                             "runs_per_ip": "6", "switch_threshold_ms": "42",
                                             "switch_threshold_percent": "7"}, follow_redirects=True)
    assert created.status_code == 200 and "example.com" in created.text
    with _database.session() as session:
        target_id = session.scalar(select(TargetRecord.id))
    edit = client.get(f"/targets/{target_id}/edit")
    assert edit.status_code == 200 and 'value="4.0"' in edit.text and "recommend" in edit.text
    updated = client.post(f"/targets/{target_id}/edit", data={"hostname": "changed.example",
                                                                "enabled": "true", "mode": "auto",
                                                                "interval_hours": "3", "runs_per_ip": "5",
                                                                "switch_threshold_ms": "50",
                                                                "switch_threshold_percent": "5"},
                          follow_redirects=True)
    assert updated.status_code == 200 and "changed.example" in updated.text
    invalid = client.post("/targets", data={"hostname": "not a hostname", "mode": "auto"})
    assert invalid.status_code == 422 and "not a hostname" in invalid.text


def test_target_detail_history_and_no_secrets_in_html(web):
    client, database = web
    with database.session() as session:
        target = save_target(session, Target(hostname="detail.example"))
        result = BenchmarkResult(ip="1.2.3.4", valid_runs=10, requested_runs=10, healthy=True,
                                average_ms=10, median_ms=9, min_ms=8, max_ms=12, jitter_ms=1)
        decision = DecisionResult(action="KEEP", current_ip="1.2.3.4", reason="Current is best")
        run = save_benchmark_run(session, target.id, [result],
                                 {"public_ips": ["1.2.3.4"], "candidate_ips": ["1.2.3.4"]}, decision)
        save_optimizer_state(session, target.id, "1.2.3.4", PendingCandidateState(), decision)
        add_rewrite_history(session, target.id, None, "1.2.3.4", "Initial rewrite", run.id)
        target_id = target.id
    detail = client.get(f"/targets/{target_id}")
    assert detail.status_code == 200
    assert "1.2.3.4" in detail.text and "Current is best" in detail.text
    assert "Run Now" in detail.text and "Initial rewrite" in detail.text
    assert "Rollback latest rewrite" in detail.text and "target-detail-panel" in detail.text
    assert "ui-secret" not in detail.text and "admin" not in detail.text
    settings = client.get("/settings")
    assert settings.status_code == 200 and "AdGuard URL" in settings.text
    assert "ui-secret" not in settings.text and "admin" not in settings.text
    history = client.get("/history")
    assert history.status_code == 200 and "detail.example" in history.text


def test_ui_formats_database_timestamps_in_vietnam_time(web):
    from datetime import datetime, timezone

    client, database = web
    utc_instant = datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc)
    expected = "27-09-2026 10:00:00 VNTime"
    with database.session() as session:
        target = save_target(session, Target(hostname="timezone.example"))
        run = save_benchmark_run(session, target.id, [], {}, None)
        run.completed_at = utc_instant
        event = add_rewrite_history(session, target.id, None, "192.0.2.1", "test", run.id)
        event.created_at = utc_instant
        target_id = target.id

    dashboard = client.get("/").text
    detail = client.get(f"/targets/{target_id}").text
    history = client.get("/history").text
    assert expected in dashboard
    assert expected in detail
    assert expected in history


def test_target_pages_not_found_and_manual_run_redirect(web):
    client, _database = web
    assert client.get("/targets/999999").status_code == 404
    assert client.get("/targets/999999/edit").status_code == 404
    created = client.post("/targets", data={"hostname": "run.example"}, follow_redirects=False)
    assert created.status_code == 303
    assert client.post("/targets/1/run", follow_redirects=False).status_code == 303


def test_apply_best_button_requires_verified_current_and_healthy_different_best(web, monkeypatch):
    client, database = web
    from app.core import optimizer

    for index, (lookup_ok, best_healthy, best_ip) in enumerate((
            (True, True, "192.0.2.2"), (False, True, "192.0.2.2"),
            (True, False, "192.0.2.2"), (True, True, "192.0.2.1"))):
        with database.session() as session:
            target = save_target(session, Target(hostname=f"apply-{index}.example"))
            current = BenchmarkResult(ip="192.0.2.1", healthy=True, valid_runs=10, requested_runs=10,
                                      average_ms=20, median_ms=20, min_ms=18, max_ms=22, jitter_ms=2)
            results = [current]
            if best_ip != current.ip:
                results.append(BenchmarkResult(ip=best_ip, healthy=best_healthy, valid_runs=10,
                                               requested_runs=10, average_ms=10, median_ms=10,
                                               min_ms=9, max_ms=11, jitter_ms=1))
            run = save_benchmark_run(session, target.id, results, {
                "current_rewrite_ip": "192.0.2.1",
                "current_rewrite_lookup_succeeded": lookup_ok,
            }, DecisionResult(action="UPDATE", current_ip="192.0.2.1", candidate_ip=best_ip,
                              reason="Candidate is faster"))
            target_id = target.id
        page = client.get(f"/targets/{target_id}").text
        should_apply = lookup_ok and best_healthy and best_ip != "192.0.2.1"
        assert ("Apply Best IP" in page) is should_apply
        targets_page = client.get("/targets").text
        inline_match = re.search(rf'<div id="run-result-{target_id}"[^>]*>(.*?)</div></td>',
                                 targets_page, re.S)
        assert inline_match and ("Apply Best IP" in inline_match.group(1)) is should_apply
        polled = client.get(f"/targets/{target_id}/run/state").text
        assert ("Apply Best IP" in polled) is should_apply
        if lookup_ok and best_healthy and best_ip != "192.0.2.1":
            assert "from 192.0.2.1 to 192.0.2.2" in page


def test_inline_and_standalone_share_apply_button_template(web):
    template_dir = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"
    partial = (template_dir / "benchmark_result.html").read_text(encoding="utf-8")
    for name in ("targets.html", "target_detail.html", "run_state.html"):
        source = (template_dir / name).read_text(encoding="utf-8")
        assert '{% include "benchmark_result.html" %}' in source
        assert "Apply Best IP" not in source
    assert partial.count("Apply Best IP") == 1


def test_apply_button_forms_are_native_post_forms_in_inline_and_detail_paths(web, monkeypatch):
    client, database = web
    import app.web.routes as web_routes
    from html.parser import HTMLParser

    with database.session() as session:
        target = save_target(session, Target(hostname="form-owner.example"))
        run = save_benchmark_run(session, target.id, [
            BenchmarkResult(ip="192.0.2.1", healthy=True, valid_runs=10, requested_runs=10,
                            average_ms=20, median_ms=20, min_ms=18, max_ms=22, jitter_ms=2),
            BenchmarkResult(ip="192.0.2.2", healthy=True, valid_runs=10, requested_runs=10,
                            average_ms=10, median_ms=10, min_ms=9, max_ms=11, jitter_ms=1),
        ], {"current_rewrite_ip": "192.0.2.1", "current_rewrite_lookup_succeeded": True},
            DecisionResult(action="UPDATE", current_ip="192.0.2.1", candidate_ip="192.0.2.2",
                           reason="Candidate is faster"))
        target_id, run_id = target.id, run.id
    monkeypatch.setattr(web_routes, "read_adguard_rewrite", lambda _hostname: ("192.0.2.1", True))
    calls = []

    def applied(session, target, new_ip, _reason, _run_id, **_kwargs):
        from app.db.models import OptimizerStateRecord
        calls.append((target.hostname, new_ip))
        state = session.get(OptimizerStateRecord, target.id)
        if state is None:
            state = OptimizerStateRecord(target_id=target.id)
            session.add(state)
        state.current_rewrite_ip = new_ip
        session.commit()
        return {"healthy": True, "changed": True, "verified_current_ip": new_ip}

    monkeypatch.setattr(web_routes, "set_rewrite", applied)

    class FormOwnerParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.forms = []
            self.apply_owner = None
            self.apply_button_type = None
            self.nested_apply = False
            self.apply_controls = set()

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "form":
                if self.forms and self.forms[-1].get("id") == f"apply-best-form-{target_id}":
                    self.nested_apply = True
                self.forms.append(attrs)
                if attrs.get("id") == f"apply-best-form-{target_id}":
                    self.apply_owner = attrs
            elif tag == "input" and self.forms and self.forms[-1].get("id") == f"apply-best-form-{target_id}":
                self.apply_controls.add(attrs.get("name"))
            elif tag == "button" and self.forms and self.forms[-1].get("id") == f"apply-best-form-{target_id}":
                self.apply_button_type = attrs.get("type")

        def handle_endtag(self, tag):
            if tag == "form" and self.forms:
                self.forms.pop()

    rendered_pages = (client.get("/targets"), client.get(f"/targets/{target_id}"))
    for page in rendered_pages:
        assert page.status_code == 200
        parser = FormOwnerParser()
        parser.feed(page.text)
        assert parser.apply_owner is not None
        assert parser.apply_owner.get("method", "get").lower() == "post"
        assert parser.apply_owner.get("action") == f"/targets/{target_id}/apply-best"
        assert parser.apply_owner.get("hx-boost") == "false"
        assert {"csrf_token", "run_id", "old_ip", "new_ip", "confirm"} <= parser.apply_controls
        assert parser.apply_button_type == "submit"
        assert parser.nested_apply is False
        response = client.post(parser.apply_owner["action"], data={
            "csrf_token": client.headers["x-csrf-token"], "run_id": run_id,
            "old_ip": "192.0.2.1", "new_ip": "192.0.2.2", "confirm": "true",
        }, follow_redirects=False)
        assert response.status_code == 303
    assert calls == [("form-owner.example", "192.0.2.2")] * 2


def test_apply_best_requires_confirmation_and_uses_existing_rewrite_service(web, monkeypatch):
    client, database = web
    import app.web.routes as web_routes

    with database.session() as session:
        target = save_target(session, Target(hostname="apply.example"))
        run = save_benchmark_run(session, target.id, [
            BenchmarkResult(ip="192.0.2.1", healthy=True, valid_runs=10, requested_runs=10,
                            average_ms=20, median_ms=20, min_ms=18, max_ms=22, jitter_ms=2),
            BenchmarkResult(ip="192.0.2.2", healthy=True, valid_runs=10, requested_runs=10,
                            average_ms=10, median_ms=10, min_ms=9, max_ms=11, jitter_ms=1),
        ], {"current_rewrite_ip": "192.0.2.1", "current_rewrite_lookup_succeeded": True},
            DecisionResult(action="UPDATE", current_ip="192.0.2.1", candidate_ip="192.0.2.2",
                           reason="Candidate is faster"))
        target_id, run_id = target.id, run.id
    monkeypatch.setattr(web_routes, "read_adguard_rewrite", lambda _hostname: ("192.0.2.1", True))
    calls = []
    def apply_verified(session, target, ip, reason, selected_run, **kwargs):
        from app.db.models import OptimizerStateRecord
        calls.append((ip, reason, selected_run, kwargs.get("expected_old_ip")))
        state = session.get(OptimizerStateRecord, target.id)
        if state is None:
            state = OptimizerStateRecord(target_id=target.id)
            session.add(state)
        state.current_rewrite_ip = ip
        session.commit()
        return {"healthy": True, "changed": True, "verified_current_ip": ip}

    monkeypatch.setattr(web_routes, "set_rewrite", apply_verified)
    form = {"run_id": run_id, "old_ip": "192.0.2.1", "new_ip": "192.0.2.2", "confirm": "true"}
    assert client.post(f"/targets/{target_id}/apply-best", data={**form, "confirm": "false"}).status_code == 400
    response = client.post(f"/targets/{target_id}/apply-best", data=form, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].endswith(
        "?rewrite=applied&old_ip=192.0.2.1&new_ip=192.0.2.2")
    assert calls == [("192.0.2.2", "Manual Apply Best IP", run_id, "192.0.2.1")]
    refreshed = client.get(response.headers["location"])
    assert "DNS rewrite updated: 192.0.2.1 -&gt; 192.0.2.2" in refreshed.text
    assert 'data-copy="192.0.2.2"' in refreshed.text
    with database.session() as session:
        from app.db.models import OptimizerStateRecord
        assert session.get(OptimizerStateRecord, target_id).current_rewrite_ip == "192.0.2.2"

    monkeypatch.setattr(web_routes, "set_rewrite", lambda *_args, **_kwargs:
                        {"healthy": False, "rolled_back": True,
                         "verified_current_ip": "192.0.2.1"})
    failed = client.post(f"/targets/{target_id}/apply-best", data=form, follow_redirects=False)
    assert failed.status_code == 303 and failed.headers["location"].endswith(
        "?rewrite=rolled-back&verified_ip=192.0.2.1")
    failed_page = client.get(failed.headers["location"])
    assert "New IP failed health check; rolled back to 192.0.2.1" in failed_page.text
    assert 'data-copy="192.0.2.1"' in failed_page.text


def test_apply_best_csrf_is_enforced(web):
    client, database = web
    with database.session() as session:
        target = save_target(session, Target(hostname="apply-csrf.example"))
        run = save_benchmark_run(session, target.id, [], {})
        target_id, run_id = target.id, run.id
    csrf = client.headers.pop("x-csrf-token")
    response = client.post(f"/targets/{target_id}/apply-best", data={
        "run_id": run_id, "old_ip": "192.0.2.1", "new_ip": "192.0.2.2", "confirm": "true",
    })
    assert response.status_code == 403
    client.headers["x-csrf-token"] = csrf


def _seed_add_dns_target(database, hostname="add-dns.example", healthy=True):
    from datetime import datetime, timezone
    with database.session() as session:
        target = save_target(session, Target(hostname=hostname))
        result = BenchmarkResult(ip="192.0.2.44", healthy=healthy, valid_runs=10 if healthy else 0,
                                requested_runs=10, average_ms=10 if healthy else None,
                                median_ms=10 if healthy else None, min_ms=9 if healthy else None,
                                max_ms=11 if healthy else None, jitter_ms=1 if healthy else None)
        run = save_benchmark_run(session, target.id, [result], {
            "current_rewrite_ip": None, "current_rewrite_lookup_succeeded": True,
            "resolution_failed": False,
        }, DecisionResult(action="KEEP", reason="test"))
        target_id, run_id = target.id, run.id
    return target_id, run_id


def test_add_to_dns_button_requires_absent_live_rewrite_and_healthy_best(web, monkeypatch):
    client, database = web
    import app.web.routes as web_routes
    target_id, _ = _seed_add_dns_target(database)
    class FakeAdGuard:
        existing = None
        def get_rewrite(self, _hostname): return self.existing
        def close(self): pass
    fake = FakeAdGuard()
    monkeypatch.setattr(web_routes, "configured_adguard_client", lambda: fake)
    assert "Add to DNS" in client.get(f"/targets/{target_id}").text
    fake.existing = {"domain": "add-dns.example", "answer": "192.0.2.44"}
    assert "Add to DNS" not in client.get(f"/targets/{target_id}").text


@pytest.mark.parametrize("path_kind", ["inline", "detail"])
def test_add_to_dns_unavailable_current_best_healthy_is_visible_in_both_paths(web, monkeypatch, path_kind):
    client, database = web
    import app.web.routes as web_routes
    target_id, run_id = _seed_add_dns_target(database)
    with database.session() as session:
        run = session.get(BenchmarkRunRecord, run_id)
        run.summary["resolution_failed"] = True
        run.summary["current_rewrite_lookup_succeeded"] = False
    class FakeAdGuard:
        def get_rewrite(self, _hostname): return None
        def close(self): pass
    monkeypatch.setattr(web_routes, "configured_adguard_client", FakeAdGuard)
    page = client.get("/targets").text if path_kind == "inline" else client.get(f"/targets/{target_id}").text
    assert "Unavailable" in page
    title_group = page.split('class="benchmark-result-top"', 1)[1].split('class="benchmark-result-ip"', 1)[0]
    assert title_group.index('class="benchmark-hostname"') < title_group.index('class="add-to-dns-form"')
    assert title_group.index('class="add-to-dns-form"') < title_group.index(">Add to DNS</button>")


def test_existing_rewrite_hides_add_and_synchronizes_from_targets_inline(web, monkeypatch):
    client, database = web
    import app.web.routes as web_routes
    from app.db.models import OptimizerStateRecord
    target_id, _ = _seed_add_dns_target(database)
    class FakeAdGuard:
        def get_rewrite(self, _hostname): return {"domain": "add-dns.example", "answer": "192.0.2.88"}
        def close(self): pass
    monkeypatch.setattr(web_routes, "configured_adguard_client", FakeAdGuard)
    page = client.get("/targets").text
    assert ">Add to DNS</button>" not in page
    with database.session() as session:
        assert session.get(OptimizerStateRecord, target_id).current_rewrite_ip == "192.0.2.88"


@pytest.mark.parametrize("healthy,stale", [(False, False), (True, True)])
def test_add_to_dns_eligibility_hides_unhealthy_or_stale_in_both_pages(web, monkeypatch, healthy, stale):
    client, database = web
    import app.web.routes as web_routes
    from datetime import timedelta
    from app.db.models import BenchmarkRunRecord, utc_now
    target_id, run_id = _seed_add_dns_target(database, healthy=healthy)
    if stale:
        with database.session() as session:
            session.get(BenchmarkRunRecord, run_id).completed_at = utc_now() - timedelta(hours=2)
    class FakeAdGuard:
        def get_rewrite(self, _hostname): return None
        def close(self): pass
    monkeypatch.setattr(web_routes, "configured_adguard_client", FakeAdGuard)
    for page in (client.get("/targets").text, client.get(f"/targets/{target_id}").text):
        assert ">Add to DNS</button>" not in page


def test_add_to_dns_creates_verifies_and_persists_once(web, monkeypatch):
    client, database = web
    import app.web.routes as web_routes
    from app.db.models import OptimizerStateRecord
    target_id, run_id = _seed_add_dns_target(database)
    class FakeAdGuard:
        existing = None
        adds = 0
        def get_rewrite(self, hostname):
            return self.existing
        def add_rewrite(self, hostname, ip):
            self.adds += 1
            self.existing = {"domain": hostname, "answer": ip}
        def close(self): pass
    fake = FakeAdGuard()
    monkeypatch.setattr(web_routes, "configured_adguard_client", lambda: fake)
    monkeypatch.setattr(web_routes, "_healthy", lambda *_args: True)
    response = client.post(f"/targets/{target_id}/add-to-dns",
                           data={"run_id": run_id, "confirm": "true"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].endswith("?rewrite=added")
    assert fake.adds == 1
    page = client.get(response.headers["location"])
    assert "DNS rewrite added and verified" in page.text
    assert 'data-copy="192.0.2.44"' in page.text
    assert ">Add to DNS</button>" not in page.text
    with database.session() as session:
        assert session.get(OptimizerStateRecord, target_id).current_rewrite_ip == "192.0.2.44"
        history = session.scalars(select(RewriteHistoryRecord).where(
            RewriteHistoryRecord.target_id == target_id)).all()
        assert len(history) == 1 and history[0].old_ip is None and history[0].new_ip == "192.0.2.44"
        assert session.scalar(select(AuditLogRecord).where(
            AuditLogRecord.event == "rewrite_added", AuditLogRecord.target_id == target_id)) is not None


def test_add_to_dns_duplicate_syncs_existing_ip_without_add(web, monkeypatch):
    client, database = web
    import app.web.routes as web_routes
    from app.db.models import OptimizerStateRecord
    target_id, run_id = _seed_add_dns_target(database)
    class FakeAdGuard:
        adds = 0
        def get_rewrite(self, _hostname): return {"domain": "add-dns.example", "answer": "192.0.2.55"}
        def add_rewrite(self, *_args): self.adds += 1
        def close(self): pass
    fake = FakeAdGuard()
    monkeypatch.setattr(web_routes, "configured_adguard_client", lambda: fake)
    monkeypatch.setattr(web_routes, "_healthy", lambda *_args: pytest.fail("duplicate must not create"))
    response = client.post(f"/targets/{target_id}/add-to-dns",
                           data={"run_id": run_id, "confirm": "true"}, follow_redirects=False)
    assert response.headers["location"].endswith("?rewrite=already-exists") and fake.adds == 0
    assert "Domain already exists in AdGuard. DRO state was synchronized." in client.get(response.headers["location"]).text
    with database.session() as session:
        assert session.get(OptimizerStateRecord, target_id).current_rewrite_ip == "192.0.2.55"
        assert not session.scalars(select(RewriteHistoryRecord).where(
            RewriteHistoryRecord.target_id == target_id)).all()


@pytest.mark.parametrize("healthy,stale", [(False, False), (True, True)])
def test_add_to_dns_blocks_unhealthy_or_stale_best(web, monkeypatch, healthy, stale):
    client, database = web
    import app.web.routes as web_routes
    from datetime import timedelta
    from app.db.models import BenchmarkRunRecord, utc_now
    target_id, run_id = _seed_add_dns_target(database, healthy=healthy)
    if stale:
        with database.session() as session:
            run = session.get(BenchmarkRunRecord, run_id)
            run.completed_at = utc_now() - timedelta(hours=2)
    class FakeAdGuard:
        adds = 0
        def get_rewrite(self, _hostname): return None
        def add_rewrite(self, *_args): self.adds += 1
        def close(self): pass
    fake = FakeAdGuard()
    monkeypatch.setattr(web_routes, "configured_adguard_client", lambda: fake)
    monkeypatch.setattr(web_routes, "_healthy", lambda *_args: True)
    response = client.post(f"/targets/{target_id}/add-to-dns",
                           data={"run_id": run_id, "confirm": "true"}, follow_redirects=False)
    assert response.status_code == 409 and fake.adds == 0


def test_add_to_dns_failure_does_not_persist_current_ip(web, monkeypatch):
    client, database = web
    import app.web.routes as web_routes
    from app.db.models import OptimizerStateRecord
    from app.integrations.adguard import AdGuardError
    target_id, run_id = _seed_add_dns_target(database)
    class FakeAdGuard:
        def get_rewrite(self, _hostname): return None
        def add_rewrite(self, *_args): raise AdGuardError("failed")
        def close(self): pass
    monkeypatch.setattr(web_routes, "configured_adguard_client", FakeAdGuard)
    monkeypatch.setattr(web_routes, "_healthy", lambda *_args: True)
    response = client.post(f"/targets/{target_id}/add-to-dns",
                           data={"run_id": run_id, "confirm": "true"}, follow_redirects=False)
    assert response.headers["location"].endswith("?rewrite=add-failed")
    assert "No success was recorded" in client.get(response.headers["location"]).text
    with database.session() as session:
        state = session.get(OptimizerStateRecord, target_id)
        assert state is None or state.current_rewrite_ip is None
        assert session.scalar(select(AuditLogRecord).where(
            AuditLogRecord.event == "rewrite_add_failed", AuditLogRecord.target_id == target_id)) is not None


def test_add_to_dns_requires_auth_csrf_and_explicit_confirmation(web):
    client, database = web
    target_id, run_id = _seed_add_dns_target(database)
    csrf = client.headers["x-csrf-token"]
    client.headers.pop("x-csrf-token")
    assert client.post(f"/targets/{target_id}/add-to-dns",
                       data={"run_id": run_id, "confirm": "true"}).status_code == 403
    client.headers["x-csrf-token"] = "invalid"
    assert client.post(f"/targets/{target_id}/add-to-dns",
                       data={"run_id": run_id, "confirm": "true"}).status_code == 403
    client.headers["x-csrf-token"] = csrf
    assert client.post(f"/targets/{target_id}/add-to-dns",
                       data={"run_id": run_id, "confirm": "false"}).status_code == 400


def test_apply_uses_verified_readback_instead_of_requested_ip(web, monkeypatch):
    client, database = web
    import app.web.routes as web_routes

    with database.session() as session:
        target = save_target(session, Target(hostname="readback.example"))
        run = save_benchmark_run(session, target.id, [
            BenchmarkResult(ip="192.0.2.1", healthy=True, valid_runs=10, requested_runs=10,
                            average_ms=20, median_ms=20, min_ms=18, max_ms=22, jitter_ms=2),
            BenchmarkResult(ip="192.0.2.2", healthy=True, valid_runs=10, requested_runs=10,
                            average_ms=10, median_ms=10, min_ms=9, max_ms=11, jitter_ms=1),
        ], {"current_rewrite_ip": "192.0.2.1", "current_rewrite_lookup_succeeded": True},
            DecisionResult(action="UPDATE", current_ip="192.0.2.1", candidate_ip="192.0.2.2",
                           reason="Candidate is faster"))
        target_id, run_id = target.id, run.id
    monkeypatch.setattr(web_routes, "read_adguard_rewrite", lambda _hostname: ("192.0.2.1", True))

    def failed_with_verified_current(session, _target, *_args, **_kwargs):
        from app.db.models import OptimizerStateRecord
        state = OptimizerStateRecord(target_id=target_id, current_rewrite_ip="192.0.2.9")
        session.add(state)
        session.commit()
        return {"healthy": False, "rolled_back": False,
                "verified_current_ip": "192.0.2.9"}

    monkeypatch.setattr(web_routes, "set_rewrite", failed_with_verified_current)
    response = client.post(f"/targets/{target_id}/apply-best", data={
        "run_id": run_id, "old_ip": "192.0.2.1", "new_ip": "192.0.2.2", "confirm": "true",
    }, follow_redirects=False)
    page = client.get(response.headers["location"])
    assert "DNS rewrite update failed: AdGuard readback did not match the requested IP" in page.text
    assert 'data-copy="192.0.2.9"' in page.text
    current_row = page.text.split('<span>Current IP:</span>', 1)[1].split('</div>', 1)[0]
    assert 'data-copy="192.0.2.9"' in current_row
    assert 'data-copy="192.0.2.2"' not in current_row


def test_settings_default_interval_conversion_applies_only_to_new_targets(web):
    client, database = web
    invalid = client.post("/settings/default-interval", data={"value": "0", "unit": "minutes"})
    assert invalid.status_code == 422 and "positive interval" in invalid.text
    conversions = [("30", "minutes", 0.5), ("2", "hours", 2.0), ("6", "hours", 6.0)]
    for index, (value, unit, expected_hours) in enumerate(conversions):
        response = client.post("/settings/default-interval", data={"value": value, "unit": unit},
                               follow_redirects=False)
        assert response.status_code == 303
        created = client.post("/api/v1/targets", json={"hostname": f"default-{index}.example"})
        assert created.status_code == 201
        assert created.json()["interval_hours"] == expected_hours

    form_page = client.get("/targets")
    assert 'name="interval_hours" min="0" step="any" required value="6.0"' in form_page.text
    client.post("/settings/default-interval", data={"value": "30", "unit": "minutes"})
    created_from_form = client.post("/targets", data={"hostname": "form-default.example"},
                                    follow_redirects=False)
    assert created_from_form.status_code == 303
    with database.session() as session:
        form_target = session.scalar(select(TargetRecord).where(TargetRecord.hostname == "form-default.example"))
        assert form_target and form_target.interval_hours == 0.5

    custom = client.post("/api/v1/targets", json={"hostname": "custom-interval.example",
                                                    "interval_hours": 4.0}).json()
    client.post("/settings/default-interval", data={"value": "30", "unit": "minutes"})
    assert client.get(f"/api/v1/targets/{custom['id']}").json()["interval_hours"] == 4.0
    with database.session() as session:
        assert get_setting(session, "default_interval_value") == "30.0"
        assert get_setting(session, "default_interval_unit") == "minutes"


def test_scheduler_setting_persists_and_run_now_works_when_disabled(web):
    client, database = web
    target = client.post("/api/v1/targets", json={"hostname": "manual-while-paused.example"}).json()
    calls = []
    client.app.state.benchmark_cycle = lambda *_args: calls.append("run") or {}
    with database.session() as session:
        assert get_setting(session, "scheduler_enabled") is None
    assert "Disabled" in client.get("/settings").text
    response = client.post(f"/targets/{target['id']}/run", follow_redirects=False)
    assert response.status_code == 303
    deadline = time.monotonic() + 2
    while not calls and time.monotonic() < deadline:
        time.sleep(.01)
    assert calls == ["run"]

    client.post("/settings/scheduler", data={"enabled": "true"})
    with database.session() as session:
        assert get_setting(session, "scheduler_enabled") == "true"
    assert "Enabled" in client.get("/settings").text
    client.post("/settings/scheduler", data={"enabled": "false"})
    with database.session() as session:
        assert get_setting(session, "scheduler_enabled") == "false"
    assert "Disabled" in client.get("/settings").text


def test_log_retention_setting_persists_and_rejects_less_than_one_day(web):
    client, database = web
    saved = client.post("/settings/log-retention", data={"days": "14"}, follow_redirects=False)
    assert saved.status_code == 303
    with database.session() as session:
        assert get_setting(session, "log_retention_days") == "14"


def test_benchmark_history_retention_hours_days_and_validation(web):
    client, database = web
    page = client.get("/")
    assert 'value="72"' in page.text and 'value="hours" selected' in page.text
    response = client.post("/settings/benchmark-history-retention",
                           data={"value": "5", "unit": "days"}, follow_redirects=False)
    assert response.status_code == 303
    with database.session() as session:
        assert get_setting(session, "benchmark_history_retention_value") == "5"
        assert get_setting(session, "benchmark_history_retention_unit") == "days"
        from app.db.retention import benchmark_history_retention
        from datetime import timedelta
        assert benchmark_history_retention(session) == timedelta(days=5)
    invalid = client.post("/settings/benchmark-history-retention",
                          data={"value": "0", "unit": "hours"})
    assert invalid.status_code == 422 and "positive whole number" in invalid.text


def test_clear_history_removes_benchmark_data_only_and_keeps_rewrite_and_audit(web):
    client, database = web
    with database.session() as session:
        target = save_target(session, Target(hostname="clear-history.example"))
        from app.db.repositories import set_setting
        set_setting(session, "preserve_setting", "kept")
        run = BenchmarkRunRecord(target_id=target.id)
        session.add(run)
        session.flush()
        result = BenchmarkResultRecord(run_id=run.id, ip="192.0.2.1", valid_runs=1,
                                       requested_runs=1, healthy=True)
        session.add(result)
        session.flush()
        session.add(BenchmarkSampleRecord(result_id=result.id, run_number=1))
        rewrite = add_rewrite_history(session, target.id, None, "192.0.2.1", "keep", run.id)
        rewrite_id = rewrite.id
        prior_audit = add_audit_event_for_test(session, target.id)
        prior_audit_id = prior_audit.id
        run_id = run.id
    history_page = client.get("/history")
    assert "Clear history" in history_page.text
    assert "Delete all benchmark history? Rewrite history and audit logs will be kept." in history_page.text
    cleared = client.post("/history/clear", data={"confirm": "true"}, follow_redirects=False)
    assert cleared.status_code == 303
    with database.session() as session:
        assert session.get(BenchmarkRunRecord, run_id) is None
        assert session.scalar(select(BenchmarkResultRecord.id)) is None
        assert session.scalar(select(BenchmarkSampleRecord.id)) is None
        kept_rewrite = session.get(RewriteHistoryRecord, rewrite_id)
        assert kept_rewrite is not None and kept_rewrite.benchmark_run_id is None
        assert session.get(AuditLogRecord, prior_audit_id) is not None
        assert session.get(TargetRecord, target.id) is not None
        assert get_setting(session, "preserve_setting") == "kept"
        assert session.scalar(select(AuditLogRecord.id).where(
            AuditLogRecord.event == "benchmark_history_cleared")) is not None


def add_audit_event_for_test(session, target_id):
    from app.db.repositories import add_audit_event
    return add_audit_event(session, "preexisting_audit", target_id)
    page = client.get("/settings")
    assert 'name="days" min="1"' in page.text and 'value="14"' in page.text

    invalid = client.post("/settings/log-retention", data={"days": "0"})
    assert invalid.status_code == 422 and "at least 1 day" in invalid.text
    with database.session() as session:
        assert get_setting(session, "log_retention_days") == "14"


def test_run_now_shows_benchmark_result_without_rewrite(web, monkeypatch):
    client, database = web
    with database.session() as session:
        target = save_target(session, Target(hostname="manual-run.example", mode="auto"))
        target_id = target.id
    current = BenchmarkResult(ip="192.0.2.21", valid_runs=10, requested_runs=10, healthy=True,
                              average_ms=20, median_ms=19, min_ms=18, max_ms=22, jitter_ms=2)
    best = BenchmarkResult(ip="192.0.2.22", valid_runs=10, requested_runs=10, healthy=True,
                           average_ms=10, median_ms=9, min_ms=8, max_ms=12, jitter_ms=1)
    decision = DecisionResult(action="UPDATE", current_ip=current.ip, candidate_ip=best.ip,
                              reason="Candidate is materially faster")

    def benchmark_cycle(session, target):
        run = save_benchmark_run(session, target.id, [current, best], {
            "current_rewrite_ip": current.ip,
            "public_ips": [current.ip, best.ip],
            "candidate_ips": [current.ip, best.ip],
        }, decision)
        save_optimizer_state(session, target.id, current.ip,
                             PendingCandidateState(candidate_ip=best.ip, consecutive_wins=2), decision)
        return {"benchmark_run_id": run.id, "decision": decision.model_dump()}

    client.app.state.benchmark_cycle = benchmark_cycle
    monkeypatch.setattr(rewrites, "set_rewrite", lambda *_args, **_kwargs: pytest.fail("DNS write attempted"))
    response = client.post(f"/targets/{target_id}/run", headers={"HX-Request": "true"})
    assert response.status_code == 200
    deadline = time.monotonic() + 3
    while client.app.state.run_coordinator.state(target_id)[0] is not None and time.monotonic() < deadline:
        time.sleep(.01)
    detail = client.get(f"/targets/{target_id}/run/state").text
    for expected in (current.ip, best.ip, "Best IP", "Avg", "Median", "Min", "Max", "Jitter",
                     "UPDATE", "Candidate is materially faster", "manual-run.example",
                     "Current IP", "Discovered candidates", "Run Timestamp"):
        assert expected in detail
    assert 'class="best-candidate"' in detail and 'data-run-id="1"' in detail
    assert detail.count('data-copy-title="Copy IP"') >= 5
    for ip in (current.ip, best.ip):
        assert f'data-copy="{ip}"' in detail
    assert 'title="Copy IP"' in detail and 'title="Copied"' not in detail
    copy_script = client.get("/static/targets.js").text
    assert 'button.dataset.copyTitle || "Copy hostname"' in copy_script
    assert 'navigator.clipboard.writeText(value)' in copy_script


def test_run_now_duplicate_for_running_target_reuses_state(web):
    client, database = web
    with database.session() as session:
        target = save_target(session, Target(hostname="busy.example"))
        target_id = target.id
    with client.app.state.run_coordinator.run(target_id):
        response = client.post(f"/targets/{target_id}/run", headers={"HX-Request": "true"})
        assert response.status_code == 200
        assert f'id="run-state-indicator-{target_id}"' in response.text
        assert 'disabled aria-busy="true"' in response.text


def test_manual_benchmark_queue_runs_two_then_fifo_and_updates_independent_rows(web):
    import threading
    import time

    client, database = web
    with database.session() as session:
        ids = [save_target(session, Target(hostname=f"queue-{letter}.example")).id
               for letter in "abcd"]
    coordinator = client.app.state.run_coordinator
    started = {target_id: threading.Event() for target_id in ids}
    release = {target_id: threading.Event() for target_id in ids}
    finished = {target_id: threading.Event() for target_id in ids}

    def task(target_id):
        def run():
            started[target_id].set()
            release[target_id].wait(3)
            finished[target_id].set()
        return run

    try:
        assert coordinator.submit(ids[0], task(ids[0]))[0] == "running"
        assert coordinator.submit(ids[1], task(ids[1]))[0] == "running"
        assert started[ids[0]].wait(2) and started[ids[1]].wait(2)
        assert coordinator.submit(ids[2], task(ids[2])) == ("queued", 1)
        assert coordinator.submit(ids[3], task(ids[3])) == ("queued", 2)
        assert coordinator.submit(ids[2], task(ids[2])) == ("queued", 1)
        assert len(coordinator._active) == 2

        page = client.get("/targets").text
        assert f"Queued #1" in page and f"Queued #2" in page
        for target_id in ids[2:]:
            button = page.split(f'id="play-button-{target_id}"', 1)[1].split(">", 1)[0]
            assert "disabled" in button
        assert all(f'id="run-result-{target_id}"' in page for target_id in ids)

        release[ids[0]].set()
        assert started[ids[2]].wait(2)
        assert coordinator.state(ids[3]) == ("queued", 1)
        assert coordinator.state(ids[2]) == ("running", None)
        assert coordinator.state(ids[0]) == (None, None)
        release[ids[1]].set()
        release[ids[2]].set()
        assert finished[ids[1]].wait(2) and finished[ids[2]].wait(2)
        assert started[ids[3]].wait(2)
        release[ids[3]].set()
        assert finished[ids[3]].wait(2)
    finally:
        for event in release.values():
            event.set()
