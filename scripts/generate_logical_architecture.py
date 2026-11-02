#!/usr/bin/env python3
"""Generate the Pre-Bidding Application Logical Solution Architecture markdown.

Reads structured architecture definitions and emits
docs/Logical-Solution-Architecture.md. Stdlib only (Python 3.9+).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "Logical-Solution-Architecture.md"


@dataclass(frozen=True)
class Decision:
    id: str
    challenge: str
    decision: str
    rationale: str
    traces: str


@dataclass(frozen=True)
class Component:
    name: str
    responsibility: str
    tech: str
    traces: str


@dataclass(frozen=True)
class Entity:
    name: str
    purpose: str
    key_attributes: str
    traces: str


@dataclass(frozen=True)
class ApiEndpoint:
    method: str
    path: str
    role: str
    purpose: str
    anonymity: str
    traces: str


DECISIONS = [
    Decision(
        id="D1",
        challenge="Application runtime language and API style for a greenfield bidding platform",
        decision="Python 3.12 with FastAPI for the HTTP API and domain services",
        rationale=(
            "FastAPI provides typed request/response models, OpenAPI generation, "
            "and async-capable handlers while keeping the concurrency-critical bid "
            "path easy to express with synchronous DB transactions."
        ),
        traces="BFR §7; FR-015–FR-031",
    ),
    Decision(
        id="D2",
        challenge="Authoritative store for items, bids, history, and audit",
        decision="PostgreSQL as the system of record",
        rationale=(
            "Relational constraints enforce one bid per user per item; "
            "row-level locks and transactions provide concurrent bid integrity "
            "required by BR-07 and FR-027–FR-031."
        ),
        traces="BR-03, BR-07; FR-027–FR-031, FR-041",
    ),
    Decision(
        id="D3",
        challenge="User authentication without custom credential management",
        decision="Google OAuth 2.0 / OpenID Connect SSO",
        rationale=(
            "BFR mandates Google SSO; OIDC yields stable subject identifiers, "
            "email, and display name for bid association (FR-009–FR-012)."
        ),
        traces="BR-02; FR-009–FR-012",
    ),
    Decision(
        id="D4",
        challenge="Source of item photographs and initial bid amounts",
        decision="Google Drive API for photos; Google Sheets API for initial amounts; local DB copies",
        rationale=(
            "Sources remain Google workspaces, but the application database owns "
            "published item state so source edits cannot rewrite bid history (FR-007, FR-008)."
        ),
        traces="BR-01; FR-002–FR-008",
    ),
    Decision(
        id="D5",
        challenge="Concurrent equal or competing bids must not violate ordering or uniqueness",
        decision=(
            "BEGIN → SELECT FOR UPDATE on BidItem → revalidate amount and uniqueness → "
            "INSERT Bid with server sequence → COMMIT"
        ),
        rationale=(
            "Transaction commit order is the authoritative bid order; "
            "item-level locking prevents lost updates on highest-bid checks."
        ),
        traces="BR-07, BR-11, BR-12; FR-027–FR-031, FR-038–FR-040",
    ),
    Decision(
        id="D6",
        challenge="Bidder privacy vs administrator transparency",
        decision="Separate public and administrator API representations of bids",
        rationale=(
            "Public responses expose amount and timestamp only; admin responses "
            "include identity fields (FR-037, FR-043–FR-045)."
        ),
        traces="BR-08, BR-09; FR-037, FR-043–FR-045",
    ),
    Decision(
        id="D7",
        challenge="Exceptional bid corrections without destroying history",
        decision="Auditable state transitions (Corrected / Cancelled) plus append-only BidAudit",
        rationale=(
            "Original amounts remain queryable; highest bid and winner are "
            "recalculated from valid bids only (FR-057–FR-065; BR-15–BR-20)."
        ),
        traces="BR-15–BR-20; FR-057–FR-065",
    ),
    Decision(
        id="D8",
        challenge="Closing bidding on schedule without operator intervention",
        decision="In-process or worker-based Closure Scheduler with event timezone",
        rationale=(
            "Supports FR-050–FR-056: manual close via admin API and automatic close "
            "when scheduled time is reached."
        ),
        traces="BR-13, BR-14; FR-050–FR-056",
    ),
    Decision(
        id="D9",
        challenge="Whether administrators may also place bids",
        decision="Administrators do not place bids in the initial release",
        rationale=(
            "BFR default recommendation separates admin privileges from bidding "
            "participation to reduce conflict-of-interest risk."
        ),
        traces="BFR §5 policy note",
    ),
]

COMPONENTS = [
    Component(
        name="Web UI",
        responsibility=(
            "Screens for Available Items, Item Details & Bidding, My Bids, "
            "Administrator Dashboard, and Bid Correction & Audit."
        ),
        tech="SPA or server-rendered front end calling the FastAPI API",
        traces="BFR §6; FR-001, FR-015, FR-047, FR-066–FR-074",
    ),
    Component(
        name="FastAPI API",
        responsibility=(
            "HTTP boundary: auth middleware, role checks, DTO mapping "
            "(public vs admin bid views), OpenAPI contract."
        ),
        tech="Python 3.12, FastAPI, Pydantic",
        traces="FR-009–FR-014; D6",
    ),
    Component(
        name="Auth (OIDC)",
        responsibility=(
            "Google SSO login, session/token validation, user provisioning "
            "from Google identity, role assignment (Bidder / Administrator)."
        ),
        tech="Authlib / google-auth; JWT or server session",
        traces="BR-02; FR-009–FR-013",
    ),
    Component(
        name="Bidding Service",
        responsibility=(
            "Submit bid, concurrency control, highest-bid calculation, "
            "tie-break by earliest sequence, My Bids status, multi-item bidding, "
            "winner finalization on close."
        ),
        tech="Python domain module + SQLAlchemy/psycopg transactions",
        traces="BR-03, BR-04, BR-05, BR-06, BR-07, BR-08, BR-09, BR-10, BR-11, BR-12, BR-19, BR-20; FR-015–FR-049",
    ),
    Component(
        name="Item Sync Service",
        responsibility=(
            "Manual/scheduled sync from Drive and Sheets; validate photo + "
            "initial amount + unique id; publish items without mutating bid history."
        ),
        tech="Google API client libraries; sync job",
        traces="BR-01; FR-001–FR-008, FR-068",
    ),
    Component(
        name="Admin Service",
        responsibility=(
            "Publish items, monitor sync, close bidding, correct/cancel bids "
            "with mandatory reason, audit and export reports."
        ),
        tech="Python domain module",
        traces="FR-050–FR-074; BR-15–BR-18",
    ),
    Component(
        name="Closure Scheduler",
        responsibility=(
            "Poll or wake on scheduled closing times; transition Open → Closed "
            "in event timezone; reject further bids."
        ),
        tech="APScheduler or dedicated worker process",
        traces="BR-13, BR-14; FR-051–FR-053",
    ),
    Component(
        name="PostgreSQL",
        responsibility=(
            "Authoritative repository for users, items, bids, schedules, "
            "sync status, and append-only audit records."
        ),
        tech="PostgreSQL 15+",
        traces="BFR §1, §7",
    ),
]

ENTITIES = [
    Entity(
        name="BidItem",
        purpose="Published pre-bid catalog entry owned by the application database",
        key_attributes=(
            "item_id (unique), name, description, photograph_ref, "
            "initial_bid_amount, status (Upcoming|Open|Closed), "
            "opening_at, closing_at, event_timezone"
        ),
        traces="FR-001–FR-007; BR-01",
    ),
    Entity(
        name="User",
        purpose="Authenticated participant identified by Google account",
        key_attributes="google_sub, email, display_name, role (Bidder|Administrator)",
        traces="FR-011–FR-013; BR-02",
    ),
    Entity(
        name="Bid",
        purpose="Accepted bid event in a transactional sequence (not a mutable amount list)",
        key_attributes=(
            "bid_id, item_id, user_id, amount, recorded_sequence, "
            "recorded_at, status (Accepted|Corrected|Cancelled)"
        ),
        traces="FR-015–FR-031, FR-041; BR-03–BR-07",
    ),
    Entity(
        name="BidAudit",
        purpose="Append-only record of administrative interventions",
        key_attributes=(
            "audit_id, bid_id, action (Correct|Cancel), original_amount, "
            "revised_amount, reason, administrator_id, action_at"
        ),
        traces="FR-057–FR-065; BR-15–BR-17",
    ),
    Entity(
        name="ItemSchedule",
        purpose="Opening/closing configuration and closure provenance",
        key_attributes=(
            "item_id, opening_at, closing_at, closure_type (Manual|Scheduled|Automatic), "
            "closed_by_user_id, closed_at, timezone"
        ),
        traces="FR-050–FR-056; BR-13–BR-14",
    ),
    Entity(
        name="ItemSynchronization",
        purpose="Drive/Sheets sync run status and errors",
        key_attributes=(
            "sync_id, source (Drive|Sheets), started_at, finished_at, "
            "status, error_detail, items_imported, items_rejected"
        ),
        traces="FR-008, FR-068",
    ),
]

API_ENDPOINTS = [
    ApiEndpoint("POST", "/auth/google/callback", "Public", "Complete Google OIDC login and establish session", "N/A", "FR-009, FR-010, FR-011, FR-012"),
    ApiEndpoint("GET", "/items", "Bidder, Admin", "List published items with status, highest bid, and bid count", "No identities", "FR-001, FR-006, FR-032, FR-033, FR-036"),
    ApiEndpoint("GET", "/items/{item_id}", "Bidder, Admin", "Item detail, photo, min bid guidance, anonymous history", "Amounts + timestamps only", "FR-005, FR-021, FR-034, FR-042, FR-043, FR-044"),
    ApiEndpoint("POST", "/items/{item_id}/bids", "Bidder", "Submit bid under transactional locking with revalidation", "Response omits other identities", "FR-015, FR-016, FR-017, FR-018, FR-019, FR-020, FR-022, FR-023, FR-024, FR-025, FR-026, FR-027, FR-028, FR-029, FR-030, FR-031"),
    ApiEndpoint("GET", "/me/bids", "Bidder, Admin", "Caller's bids and status across items", "Own identity only", "FR-047, FR-048"),
    ApiEndpoint("POST", "/admin/sync", "Admin", "Trigger Drive/Sheets synchronization", "N/A", "FR-002, FR-003, FR-004, FR-008, FR-068"),
    ApiEndpoint("POST", "/admin/items/{item_id}/publish", "Admin", "Publish validated item", "N/A", "FR-007, FR-067"),
    ApiEndpoint("POST", "/admin/items/{item_id}/close", "Admin", "Manual close with audit", "N/A", "FR-050, FR-054, FR-055"),
    ApiEndpoint("PATCH", "/admin/items/{item_id}/schedule", "Admin", "Configure scheduled closing time", "N/A", "FR-051, FR-052"),
    ApiEndpoint("GET", "/admin/items/{item_id}/bids", "Admin", "Full bid queue including bidder identities", "Identities visible", "FR-045, FR-046, FR-069, FR-070, FR-071"),
    ApiEndpoint("POST", "/admin/bids/{bid_id}/correct", "Admin", "Correct amount with mandatory reason", "Admin-only", "FR-057, FR-059, FR-060, FR-061, FR-062, FR-063, FR-064, FR-065"),
    ApiEndpoint("POST", "/admin/bids/{bid_id}/cancel", "Admin", "Cancel bid with mandatory reason", "Admin-only", "FR-058, FR-059, FR-060, FR-061, FR-062, FR-063, FR-064, FR-065"),
    ApiEndpoint("GET", "/admin/reports/winners", "Admin", "Final winning bids after closure", "Identities visible", "FR-038, FR-039, FR-040, FR-073, FR-074"),
    ApiEndpoint("GET", "/admin/audit", "Admin", "Administrative audit trail", "Identities visible", "FR-072"),
]

# Explicit FR → mechanism map (FR-001 … FR-074) for full BFR traceability.
FR_TRACEABILITY: list[tuple[str, str, str]] = [
    ("FR-001", "Display bid items", "GET /items + BidItem store"),
    ("FR-002", "Google Drive photographs", "Item Sync Service → Drive API → photograph_ref"),
    ("FR-003", "Google Sheets initial amounts", "Item Sync Service → Sheets API → initial_bid_amount"),
    ("FR-004", "Item association by unique id", "Sync join key = item_id across Drive and Sheets"),
    ("FR-005", "Item details", "GET /items/{item_id}"),
    ("FR-006", "Item status Upcoming/Open/Closed", "BidItem.status"),
    ("FR-007", "Item validation before publish", "Admin publish rejects missing photo/amount/duplicate id"),
    ("FR-008", "Manual or scheduled sync", "POST /admin/sync + sync job; ItemSynchronization"),
    ("FR-009", "Google SSO", "Auth OIDC"),
    ("FR-010", "Authenticated access to bid", "API auth middleware on POST /bids"),
    ("FR-011", "Google identity uniqueness", "User.google_sub primary key"),
    ("FR-012", "Display name and email", "User profile fields from OIDC claims"),
    ("FR-013", "Bidder and Administrator roles", "User.role + route guards"),
    ("FR-014", "Admin identity and management access", "/admin/* routes"),
    ("FR-015", "Submit bid", "POST /items/{item_id}/bids"),
    ("FR-016", "One bid per item", "UNIQUE (item_id, user_id)"),
    ("FR-017", "Multiple item bidding", "Independent bids per item_id"),
    ("FR-018", "Bid amount entry", "Request body amount field"),
    ("FR-019", "First bid ≥ initial", "Bidding Service floor check"),
    ("FR-020", "Subsequent ≥ highest valid", "Bidding Service floor check under lock"),
    ("FR-021", "Minimum bid guidance", "Computed min on item detail"),
    ("FR-022", "Reject below highest", "Service rejection path"),
    ("FR-023", "Reject duplicate user bid", "Uniqueness constraint + check"),
    ("FR-024", "Bid confirmation", "Accepted response with amount"),
    ("FR-025", "Bid timestamp", "Bid.recorded_at server time"),
    ("FR-026", "Bid immutability for bidders", "No bidder update/delete APIs"),
    ("FR-027", "Concurrent submissions", "Item-level SELECT FOR UPDATE"),
    ("FR-028", "Bid revalidation", "Re-check after lock acquired"),
    ("FR-029", "Submission ordering", "Commit order + recorded_sequence"),
    ("FR-030", "Reject stale concurrent bid", "Post-lock floor revalidation"),
    ("FR-031", "Transaction integrity", "Single DB transaction per submit"),
    ("FR-032", "Current highest bid display", "Derived from valid bids"),
    ("FR-033", "Initial amount when no bids", "Fallback to initial_bid_amount"),
    ("FR-034", "Minimum bid guidance display", "Item detail DTO"),
    ("FR-035", "Highest updates after submit/admin", "Recalculation after write"),
    ("FR-036", "Bid count", "Count of valid accepted/corrected bids"),
    ("FR-037", "Anonymous visibility", "PublicBidView"),
    ("FR-038", "Winner determination", "Max valid amount"),
    ("FR-039", "Tie-break earliest valid", "MIN(recorded_sequence) among ties"),
    ("FR-040", "Winner after close + admin fixes", "Finalize on Closed status"),
    ("FR-041", "Complete chronological history", "Bid rows ordered by sequence"),
    ("FR-042", "Display in recording order", "ORDER BY recorded_sequence"),
    ("FR-043", "Public history while open", "Authenticated GET item history"),
    ("FR-044", "Hide bidder identity publicly", "PublicBidView omits identity"),
    ("FR-045", "Admin full history", "AdminBidView"),
    ("FR-046", "Identify highest in history", "Derived leader flag"),
    ("FR-047", "My Bids", "GET /me/bids"),
    ("FR-048", "My bid status", "Highest / Outbid / Winning"),
    ("FR-049", "Post-closure history", "History retained after Closed"),
    ("FR-050", "Manual closure", "POST /admin/items/{id}/close"),
    ("FR-051", "Scheduled closure config", "PATCH schedule + ItemSchedule"),
    ("FR-052", "Automatic closure", "Closure Scheduler"),
    ("FR-053", "Reject bids after close", "Status check under lock"),
    ("FR-054", "Closure visibility", "status + closing_at on item DTOs"),
    ("FR-055", "Closure audit", "ItemSchedule closure fields"),
    ("FR-056", "Upcoming items", "status Upcoming in listings"),
    ("FR-057", "Bid correction", "POST /admin/bids/{id}/correct"),
    ("FR-058", "Bid cancellation", "POST /admin/bids/{id}/cancel"),
    ("FR-059", "Mandatory reason", "Required request field"),
    ("FR-060", "Audit trail of original and action", "BidAudit append-only"),
    ("FR-061", "Administrator identity on action", "BidAudit.administrator_id"),
    ("FR-062", "Action timestamp", "BidAudit.action_at"),
    ("FR-063", "Recalculate highest/winner", "Admin Service post-intervention"),
    ("FR-064", "Preserve original bid record", "Status transition, no hard delete"),
    ("FR-065", "Public history consistency", "Public view reflects valid state only"),
    ("FR-066", "Admin view imported items", "Admin item management APIs"),
    ("FR-067", "Publish validated items", "POST /admin/items/{id}/publish"),
    ("FR-068", "Sync status monitoring", "ItemSynchronization + admin UI"),
    ("FR-069", "Bid monitoring all items", "Admin bid listings"),
    ("FR-070", "Bidder identification for admin", "AdminBidView identity fields"),
    ("FR-071", "Bid summary", "initial, highest, count, winner"),
    ("FR-072", "Audit reporting", "GET /admin/audit"),
    ("FR-073", "Bid export", "Admin export of history/summaries"),
    ("FR-074", "Winner reporting", "GET /admin/reports/winners"),
]

BR_TRACEABILITY: list[tuple[str, str, str]] = [
    ("BR-01", "Unique id, photograph, initial amount", "Publish validation + sync"),
    ("BR-02", "Google-authenticated bidders only", "OIDC middleware"),
    ("BR-03", "One bid per user per item", "UNIQUE (item_id, user_id)"),
    ("BR-04", "Users may bid on multiple items", "No cross-item exclusivity"),
    ("BR-05", "First bid ≥ initial", "Submit validation"),
    ("BR-06", "Subsequent ≥ highest valid", "Submit validation"),
    ("BR-07", "Concurrent order = commit order", "Transactional locking"),
    ("BR-08", "All authenticated users see amounts/timestamps", "PublicBidView"),
    ("BR-09", "Identities admin-only", "AdminBidView"),
    ("BR-10", "Users see own bids/status", "GET /me/bids"),
    ("BR-11", "Equal highest bids accepted", "No uniqueness on amount"),
    ("BR-12", "Earliest valid wins ties", "recorded_sequence"),
    ("BR-13", "Manual or automatic close", "Admin API + Scheduler"),
    ("BR-14", "Closed items reject bids", "Status guard"),
    ("BR-15", "Admin correct/cancel with reason", "Admin Service + BidAudit"),
    ("BR-16", "Admin changes fully auditable", "BidAudit"),
    ("BR-17", "Original bids preserved", "No destructive deletes"),
    ("BR-18", "Recalc after admin changes", "Post-intervention recalculation"),
    ("BR-19", "Final winner after close", "Finalize on Closed"),
    ("BR-20", "Cancelled bids excluded from winner", "Filter status != Cancelled"),
]


def _table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def render() -> str:
    today = date.today().isoformat()
    decision_rows = [
        [d.id, d.challenge, d.decision, d.rationale, d.traces] for d in DECISIONS
    ]
    component_rows = [
        [c.name, c.responsibility, c.tech, c.traces] for c in COMPONENTS
    ]
    entity_rows = [
        [e.name, e.purpose, e.key_attributes, e.traces] for e in ENTITIES
    ]
    api_rows = [
        [a.method, a.path, a.role, a.purpose, a.anonymity, a.traces]
        for a in API_ENDPOINTS
    ]
    fr_rows = [[fr, summary, mechanism] for fr, summary, mechanism in FR_TRACEABILITY]
    br_rows = [[br, summary, mechanism] for br, summary, mechanism in BR_TRACEABILITY]

    return f"""# Pre-Bidding Application — Logical Solution Architecture

| Field | Value |
| --- | --- |
| Version | 1.0 |
| Date | {today} |
| Status | Draft |
| Source requirements | Pre-Bidding Application – Business Functional Requirements v3.0 (November 2026) |
| Application stack | Python 3.12, FastAPI, PostgreSQL |
| Generated by | `scripts/generate_logical_architecture.py` |

## 1. Purpose

Provide a centralized, transparent, and auditable digital platform for pre-bidding activities. Authenticated users browse items (with photographs and initial amounts), submit bids, and track status. Administrators manage periods, correct exceptional records, and export results.

This document defines the **logical** solution architecture: components, domain model, key flows, and design decisions. Physical deployment topology and detailed NFRs are out of scope here and should follow as a separate NFR document.

## 2. Business drivers and outcomes

{_table(
    ["Driver", "Outcome"],
    [
        ["Centralize pre-bidding", "Single platform replaces manual bid collection"],
        ["Authenticated participation", "Google SSO; only signed-in users bid"],
        ["Fair concurrent bidding", "Transactional ordering and revalidation"],
        ["Privacy with oversight", "Anonymous public history; admin identity visibility"],
        ["Auditability", "Append-only administrative interventions"],
        ["Deterministic winners", "Highest valid bid; earliest sequence on ties"],
    ],
)}

## 3. Scope

{_table(
    ["Category", "Details"],
    [
        [
            "In scope",
            "• Item sync from Google Drive / Sheets<br/>• Google SSO and RBAC (Bidder / Administrator)<br/>• Bid submission, history, My Bids<br/>• Concurrent bid integrity<br/>• Manual and scheduled closure<br/>• Bid correct/cancel with audit<br/>• Admin monitoring and winner export",
        ],
        [
            "Out of scope (initial release)",
            "• Online payment collection<br/>• Email / SMS / WhatsApp notifications<br/>• Multiple currencies<br/>• Proxy or automatic incremental bidding<br/>• Multiple bids per user per item<br/>• Automatic transfer of items to winners<br/>• Public access without Google authentication",
        ],
        [
            "Assumptions",
            "• Event timezone is configured once per event<br/>• Administrators do not place bids (D9)<br/>• Google Drive folder and Sheet are designated and accessible via service account or delegated credentials",
        ],
    ],
)}

## 4. Logical context

Actors and external systems collaborating with the Pre-Bidding Application.

```mermaid
flowchart TB
  subgraph actors [Actors]
    Bidder([Bidder])
    Admin([Administrator])
  end
  subgraph system [PreBiddingApplication]
    App[PreBiddingApp]
  end
  subgraph google [GoogleCloudServices]
    SSO[GoogleSSO]
    Drive[GoogleDrive]
    Sheets[GoogleSheets]
  end
  DB[(ApplicationDatabase)]
  Bidder --> App
  Admin --> App
  App --> SSO
  App --> Drive
  App --> Sheets
  App --> DB
```

## 5. Logical components

Python application modules and supporting stores.

```mermaid
flowchart TB
  subgraph actors [Actors]
    Bidder([Bidder])
    Admin([Administrator])
  end
  subgraph app [PreBiddingApp_Python]
    WebUI[Web_UI]
    Api[FastAPI_API]
    Auth[Auth_OIDC]
    BidSvc[Bidding_Service]
    ItemSvc[Item_Sync_Service]
    AdminSvc[Admin_Service]
    Scheduler[Closure_Scheduler]
  end
  subgraph data [Data]
    PG[(PostgreSQL)]
  end
  subgraph google [Google]
    SSO[Google_SSO]
    Drive[Google_Drive]
    Sheets[Google_Sheets]
  end
  Bidder --> WebUI
  Admin --> WebUI
  WebUI --> Api
  Api --> Auth
  Api --> BidSvc
  Api --> ItemSvc
  Api --> AdminSvc
  Auth --> SSO
  BidSvc --> PG
  ItemSvc --> PG
  ItemSvc --> Drive
  ItemSvc --> Sheets
  AdminSvc --> PG
  Scheduler --> BidSvc
```

{_table(
    ["Component", "Responsibility", "Technology", "Traces"],
    component_rows,
)}

### 5.1 Suggested Python package layout

```text
src/prebidding/
  main.py              # FastAPI app factory / ASGI entry
  settings.py          # 12-factor configuration
  api/                 # routers + public/admin DTOs
  auth/                # Google OIDC
  services/
    bidding.py         # submit, highest, winner, my-bids
    items_sync.py      # Drive + Sheets sync
    admin.py           # close, correct, cancel, reports
  models/              # SQLAlchemy / domain entities
  repositories/        # persistence + locking helpers
  jobs/
    closure_scheduler.py
  audit/               # append-only BidAudit writers
```

## 6. Domain model

Recommended core entities from BFR §7. Bids are **events in a transactional sequence**, not a mutable list of amounts.

```mermaid
erDiagram
  User ||--o{{ Bid : places
  BidItem ||--o{{ Bid : receives
  BidItem ||--o| ItemSchedule : scheduled_by
  BidItem ||--o{{ ItemSynchronization : synced_via
  Bid ||--o{{ BidAudit : audited_by
  User ||--o{{ BidAudit : performed_by

  User {{
    string google_sub PK
    string email
    string display_name
    string role
  }}
  BidItem {{
    string item_id PK
    string name
    string photograph_ref
    decimal initial_bid_amount
    string status
    datetime opening_at
    datetime closing_at
  }}
  Bid {{
    uuid bid_id PK
    string item_id FK
    string user_id FK
    decimal amount
    bigint recorded_sequence
    datetime recorded_at
    string status
  }}
  BidAudit {{
    uuid audit_id PK
    uuid bid_id FK
    string action
    decimal original_amount
    decimal revised_amount
    string reason
    string administrator_id FK
    datetime action_at
  }}
  ItemSchedule {{
    string item_id PK
    datetime closing_at
    string closure_type
    string timezone
  }}
  ItemSynchronization {{
    uuid sync_id PK
    string source
    string status
    string error_detail
  }}
```

{_table(
    ["Entity", "Purpose", "Key attributes", "Traces"],
    entity_rows,
)}

### 6.1 Integrity constraints

{_table(
    ["Constraint", "Implementation", "Traces"],
    [
        ["One bid per user per item", "UNIQUE (item_id, user_id) on Bid where status is not replaced by a second insert — reject duplicates at API and DB", "BR-03; FR-016, FR-023"],
        ["First bid floor", "amount >= BidItem.initial_bid_amount when no valid prior bids", "BR-05; FR-019"],
        ["Subsequent bid floor", "amount >= current highest **valid** accepted/corrected amount", "BR-06; FR-020, FR-022"],
        ["Closed items reject bids", "status == Closed checked under item lock", "BR-14; FR-053"],
        ["Cancelled bids excluded", "Winner/highest queries filter status != Cancelled", "BR-20; FR-063"],
        ["Tie-break", "Among equal highest valid amounts, MIN(recorded_sequence) wins", "BR-11, BR-12; FR-039"],
    ],
)}

## 7. Key logical flows

### 7.1 Google SSO login

```mermaid
sequenceDiagram
  participant User
  participant WebUI
  participant Api as FastAPI_API
  participant Auth as Auth_OIDC
  participant Google as Google_SSO
  participant PG as PostgreSQL
  User->>WebUI: Sign in
  WebUI->>Api: Start OIDC
  Api->>Auth: Begin login
  Auth->>Google: Authorization request
  Google-->>User: Consent
  Google-->>Auth: Auth code / tokens
  Auth->>PG: Upsert User by google_sub
  Auth-->>Api: Session established
  Api-->>WebUI: Authenticated
```

### 7.2 Bid submission under concurrency

Authoritative ordering is **successful transaction commit order** (BR-07).

```mermaid
sequenceDiagram
  participant U1 as Bidder_A
  participant U2 as Bidder_B
  participant Api as FastAPI_API
  participant BidSvc as Bidding_Service
  participant PG as PostgreSQL
  U1->>Api: POST bid amount
  U2->>Api: POST bid amount
  Api->>BidSvc: submit(A)
  Api->>BidSvc: submit(B)
  BidSvc->>PG: BEGIN
  BidSvc->>PG: SELECT BidItem FOR UPDATE
  BidSvc->>PG: Revalidate amount and uniqueness
  BidSvc->>PG: INSERT Bid sequence N
  BidSvc->>PG: COMMIT
  Note over BidSvc,PG: Loser of the lock waits then revalidates against new highest
  BidSvc-->>Api: Accepted or Rejected
  Api-->>U1: Confirmation or rejection
  Api-->>U2: Confirmation or rejection
```

**Algorithm (logical):**

1. `BEGIN`
2. `SELECT … FOR UPDATE` on the `BidItem` row
3. Reject if item is not `Open`
4. Reject if the user already has a bid for the item
5. Compute minimum acceptable amount from valid bids (or initial amount)
6. Reject if submitted amount is below the minimum
7. `INSERT` bid with server-assigned `recorded_sequence` and `recorded_at`
8. `COMMIT`
9. Return confirmation with submitted amount (FR-024)

### 7.3 Scheduled and manual closure

```mermaid
sequenceDiagram
  participant Admin
  participant Scheduler as Closure_Scheduler
  participant BidSvc as Bidding_Service
  participant PG as PostgreSQL
  Admin->>BidSvc: Manual close item
  BidSvc->>PG: Set Closed + ItemSchedule audit
  Scheduler->>BidSvc: Due closing_at reached
  BidSvc->>PG: Set Closed if still Open
  Note over BidSvc,PG: Subsequent POST /bids rejected
  BidSvc->>PG: Finalize winner from valid bids
```

### 7.4 Administrative correct / cancel

```mermaid
sequenceDiagram
  participant Admin
  participant Api as FastAPI_API
  participant AdminSvc as Admin_Service
  participant PG as PostgreSQL
  Admin->>Api: Correct or Cancel + reason
  Api->>AdminSvc: apply_intervention
  AdminSvc->>PG: BEGIN
  AdminSvc->>PG: Lock BidItem and Bid
  AdminSvc->>PG: Update Bid.status / amount
  AdminSvc->>PG: INSERT BidAudit append-only
  AdminSvc->>PG: Recalculate highest and leader
  AdminSvc->>PG: COMMIT
  AdminSvc-->>Api: Updated public and admin views
```

## 8. API surface (logical)

Public bid representations never include bidder name or email. Administrator routes may.

{_table(
    ["Method", "Path", "Roles", "Purpose", "Anonymity", "Traces"],
    api_rows,
)}

## 9. Cross-cutting concerns

{_table(
    ["Concern", "Logical approach"],
    [
        [
            "Authorization",
            "Role claims on session/token; Bidder routes vs `/admin/*` guards (FR-013, FR-014)",
        ],
        [
            "Privacy",
            "DTO split: PublicBidView (amount, timestamp, sequence) vs AdminBidView (+ identity) (BR-08, BR-09)",
        ],
        [
            "Audit",
            "Append-only BidAudit and ItemSchedule closure fields; never hard-delete bids (BR-16, BR-17)",
        ],
        [
            "Synchronization",
            "Item Sync Service copies Drive/Sheets into BidItem; invalid rows rejected; history immutable (FR-007, FR-008)",
        ],
        [
            "Scheduling",
            "Closure Scheduler evaluates closing_at in configured event timezone (FR-051–FR-053)",
        ],
        [
            "Observability (logical)",
            "Structured logs for bid accept/reject, lock wait, sync errors, admin actions; metrics for bid latency and rejection reasons — detailed targets in NFR document",
        ],
    ],
)}

## 10. Design decisions

{_table(
    ["ID", "Challenge", "Decision", "Rationale", "Traces"],
    decision_rows,
)}

## 11. Requirements coverage map

{_table(
    ["Theme", "FR / BR coverage", "Primary components"],
    [
        ["Item management & sync", "FR-001–FR-008; BR-01", "Item Sync Service, BidItem, ItemSynchronization"],
        ["Authentication & roles", "FR-009–FR-014; BR-02", "Auth OIDC, User"],
        ["Bid submit & validation", "FR-015–FR-026; BR-03–BR-06", "Bidding Service, Bid"],
        ["Concurrency", "FR-027–FR-031; BR-07", "Bidding Service, PostgreSQL locks"],
        ["Highest bid & winner", "FR-032–FR-040; BR-11, BR-12, BR-19, BR-20", "Bidding Service"],
        ["History & My Bids", "FR-041–FR-049; BR-08–BR-10", "Bidding Service, API DTOs"],
        ["Closure", "FR-050–FR-056; BR-13, BR-14", "Admin Service, Closure Scheduler"],
        ["Admin correct/cancel", "FR-057–FR-065; BR-15–BR-18", "Admin Service, BidAudit"],
        ["Admin reporting", "FR-066–FR-074", "Admin Service"],
        ["Multi-item bidding", "BR-04; FR-017", "Bidding Service (independent per item)"],
        ["Highest bid live updates", "FR-035", "Recalculation after submit and admin interventions"],
    ],
)}

### 11.1 Functional requirements traceability (FR-001 – FR-074)

{_table(["ID", "Requirement", "Architectural mechanism"], fr_rows)}

### 11.2 Business rules traceability (BR-01 – BR-20)

{_table(["ID", "Business rule", "Architectural mechanism"], br_rows)}

## 12. Acceptance scenarios (architecture readiness)

The logical design must enable these BFR §8 scenarios:

{_table(
    ["Scenario", "Architectural mechanism"],
    [
        ["Google login", "Auth OIDC + User upsert"],
        ["View items / photos / initial amounts", "Synced BidItem + Drive photograph_ref"],
        ["First / subsequent bid validation", "Bidding Service floors under item lock"],
        ["Second bid same item rejected", "UNIQUE (item_id, user_id) + service check"],
        ["Concurrent submissions", "SELECT FOR UPDATE + revalidation"],
        ["Equal highest bids", "Both accepted; earliest recorded_sequence leads"],
        ["Anonymous public history", "PublicBidView"],
        ["Admin identity visibility", "AdminBidView"],
        ["Manual / scheduled close", "Admin API + Closure Scheduler"],
        ["Correct / cancel with audit", "Bid status transition + BidAudit"],
        ["Winner after close", "Finalize from valid bids only"],
    ],
)}

## 13. Next steps

1. Produce the **Non-Functional Requirements** document (availability, latency under concurrent bidding, data retention, backup/DR, security baselines).
2. Detail OpenAPI schemas and JSON field names for public vs admin DTOs.
3. Prototype the bid-submit transaction isolation level and lock strategy in PostgreSQL.
4. Confirm Google API credential model (service account vs domain-wide delegation) with the event operators.
5. Proceed to physical architecture / deployment once NFRs are agreed.

---

*This file is generated. Re-run `python3 scripts/generate_logical_architecture.py` after updating the structured definitions in that script.*
"""


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render(), encoding="utf-8")
    print(f"Wrote {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
