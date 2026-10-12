# Should fiscl ship a QuickBooks Online read-only connector (Phase 4 slice 5)?
Date: 2026-10-11 · Author: my-skills:researcher · Requested by: orchestrator (Phase 4 R1) · Status: partial (see "Unverified")

## Answer (TL;DR)
- Cost is not the blocker: developer account, sandbox, production keys for a private app, and the Builder tier are all $0. Builder includes 500,000 CorePlus (read) calls per month; fiscl needs a few hundred. Over-cap calls are blocked (429), never billed. Confidence: high on cost (Intuit Program Guide v1.2, 03.2026).
- The real blocker is OAuth redirect rules. Production redirect URIs must be https and IP addresses are not allowed; community and Intuit-linked sources say plain localhost is rejected for production. Loopback `http://localhost:<port>` works for sandbox only. Confidence: high that sandbox works, medium-high that production localhost is rejected.
- No read-only scope exists (only `com.intuit.quickbooks.accounting`), and PKCE is not documented. Read-only must stay code-enforced, as the spec already plans.
- Production keys need a self-attested assessment questionnaire (required even for private apps, approved by Intuit), but no security/technical review unless listing on the App Store. Question wording not verified.
- Recommendation: BUILD SANDBOX-ONLY first (loopback flow), with production through the paste-refresh-token fallback, which is gated on one human check. Details below.

## Findings

### 1. Developer account, sandbox, production keys
- Developer signup is free; a sandbox company is created automatically; up to 10 sandboxes, each valid 2 years ([Create and test with a sandbox company](https://developer.intuit.com/app/developer/qbo/docs/develop/sandboxes/manage-your-sandboxes), via search snippet). Sandbox calls are not in the new fee structure (Intuit statement quoted by [Truto, 2026-03-30](https://truto.one/blog/how-much-does-the-quickbooks-api-cost-2026-pricing-rate-limits/), secondary).
- Development keys work only with sandbox companies; production keys only with live companies (third-party guides).
- Program Guide v1.2 (03.2026), "Compliance": every tier including Builder requires an "Approved assessment questionnaire and your acceptance of Terms". "To publish an App, you must first complete a self-attested assessment questionnaire. Upon approval, your App moves to production". Security, technical and marketing reviews are required only "if you aim to launch your App on the Intuit App Marketplace". Marketplace listing is optional. ([App Partner Program Guide PDF](https://static.developer.intuit.com/resources/Intuit_App_Partner_Program_Guide.pdf))
- Intuit support, in a developer-forum thread, says the questionnaire applies to private apps too and that app review is only for marketplace listing (reported via search snippet; the page itself did not render): [Will I need to prepare for an app review ... private app?](https://help.developer.intuit.com/s/question/0D5TR00000fHVqB0AW/will-i-need-to-prepare-for-an-app-review-to-get-production-keys-for-a-private-app)
- Third-party vendors report approval in minutes for the questionnaire ([Codat](https://docs.codat.io/integrations/accounting/quickbooksonline/qbo-app-assessment-questionnaire/), [Railz](https://help.railz.ai/en/articles/6395617-steps-to-complete-the-quickbooks-online-app-assessment-questionnaire)); a 2026-07 source says non-marketplace approval timelines are unpublished ([Vorp Labs](https://vorplabs.com/agent-tools/quickbooks-online-api)).
- Practical cost to a user: they must answer a questionnaire (likely incl. privacy policy / EULA / host URLs, unverified) for an app that only they use.

### 2. App Partner Program (launched 2025-07-28; full variable fees from 2025-11-01 per [Apideck](https://www.apideck.com/blog/quickbooks-api-pricing-and-the-intuit-app-partner-program); Guide v1.2 dated 03.2026)
| Tier | Monthly | CorePlus credits/mo | Overage |
|---|---|---|---|
| Builder | $0 | 500,000 (cap per calendar month) | Blocked, not billed |
| Silver | $300 | 1M | $3.50 per 1,000 (1M-10M) |
| Gold | $1,700 | 10M | $1.40 per 1,000 (10M-75M) |
| Platinum | $4,500 | 75M | $0.25 per 1,000 (75M+) |
- Core (create/update, "data-in") calls are unmetered. CorePlus ("data-out": reading accounts, querying company info, fetching reports) is metered; classification is by endpoint plus HTTP method. Only 2xx responses count. Credits aggregate across production apps in a workspace. Builder credits reset monthly. Premium APIs (Projects, Custom Fields, Sales Tax, payroll comp) need Silver+; fiscl needs none of them.
- Intuit's REST limits page also says Builder over-cap calls get HTTP 429 ([limits and throttles](https://static.developer.intuit.com/output_html/qbo/docs/learn/limits-and-throttles.html)). Apideck-style claims of a $0.25/1k Builder overage are not in Intuit's rate card; ignore.
- fiscl estimate: incremental sync of 3 entities (a few pages each) plus 2 report calls per run, even hourly, is well under 10,000 calls per month.

### 3. OAuth 2.0
- Scope: only `com.intuit.quickbooks.accounting` (plus `com.intuit.quickbooks.payment` for the Payments API) and OpenID scopes `openid profile email phone address`. No read-only accounting scope. ([OAuth 2.0](https://static.developer.intuit.com/output_html/qbo/docs/develop/authentication-and-authorization/oauth-2.0.html), [FAQ](https://static.developer.intuit.com/output_html/qbo/docs/develop/authentication-and-authorization/faq.html))
- Endpoints: authorize `https://appcenter.intuit.com/connect/oauth2`; token `https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer`; revoke `https://developer.api.intuit.com/v2/oauth2/tokens/revoke` (matches the spec's host/path plan). Discovery: production `https://developer.api.intuit.com/.well-known/openid_configuration`, sandbox `https://developer.api.intuit.com/.well-known/openid_sandbox_configuration`. I fetched `developer.intuit.com/.well-known/openid_configuration`, which listed issuer `https://oauth.platform.intuit.com/op/v1`, the same three endpoints, `scopes_supported` openid/email/profile/address/phone, and no `code_challenge_methods_supported`.
- PKCE: not documented anywhere I could reach; discovery has no `code_challenge_methods_supported`. Treat as unsupported. Token exchange uses client secret via Basic auth. Rely on `state` plus the client secret.
- Redirect URIs ([Set app redirect URIs](https://static.developer.intuit.com/output_html/qbo/docs/develop/authentication-and-authorization/set-redirect-uri.html)): separate sandbox and production lists. Server "can only redirect to URIs beginning with https". "You can add localhost (no HTTPS) as a URI for sandbox environments." "IP addresses aren't allowed." No query parameters; pass values in `state`. Intuit's docs do not state exact-match or port rules, but the Intuit MCP server README and developers say match is exact on protocol, host, port and path ([intuit/quickbooks-online-mcp-server](https://github.com/intuit/quickbooks-online-mcp-server)), and a mismatch causes `invalid_grant`.
- Production + localhost: the Intuit developer portal rejects `http://localhost` for production; workarounds are ngrok, a public HTTPS callback host, or DNS tricks (same README; Intuit help threads [1](https://help.developer.intuit.com/s/question/0D54R00007Bf8sfSAB/why-does-production-redirect-uri-not-allow-localhost), [2](https://help.developer.intuit.com/s/question/0D54R00006utYqnSAE/i-tested-the-qbo-api-functions-in-local-host-i-want-to-switch-it-to-the-production-version-however-https-website-is-need-for-the-redirect-url-i-only-use-this-api-tool-in-house-for-the-company-is-it-a-way-to-use-local-host-to-do-it-thank-you); pages did not render for me, snippets only). Consequence: spec step 2 ("127.0.0.1:<port>") is invalid even for sandbox because IP addresses are disallowed; use `http://localhost:<port>/qbo/callback`.
- Tokens: access 3,600 s. Refresh token 100-day rolling expiry (`x_refresh_token_expires_in`), 5-year hard maximum (`x_refresh_token_hard_expires_in`, only returned if request header `x-include-refresh-token-hard-expires-in: true`). Refresh tokens rotate: value changes about every 24 h or on next refresh; the previous value expires; store the latest; refresh sequentially (concurrent/reused refresh gives `invalid_grant` and may invalidate). This supports the spec's "store new refresh token before use" rule. Disconnecting the company in QuickBooks invalidates both tokens. Note fiscl must sync at least once per 100 days or the user must reauthorize.

### 4. Accounting API
- Base URLs: production `https://quickbooks.api.intuit.com/v3/company/<realmId>/...`; sandbox `https://sandbox-quickbooks.api.intuit.com/v3/company/<realmId>/...`.
- Query: documented as HTTP GET `/v3/company/<realmId>/query?query=<url-encoded SELECT>`. Grammar is `SELECT * | count(*) FROM <Entity> [WHERE] [ORDERBY] [STARTPOSITION n] [MAXRESULTS n]`; no projections, JOIN, GROUP BY or OR; max 1,000 entities per response ([Intuit blog, Deep dive SQL queries, 2017](https://blogs.a.intuit.com/2017/02/08/deep-dive-sql-queries); [limits page](https://static.developer.intuit.com/output_html/qbo/docs/learn/limits-and-throttles.html)). The grammar has no write statement, so the query language cannot mutate. A POST form of /query may exist (unverified); the spec only allows GET, which is correct.
- Reports: `GET /v3/company/<realmId>/reports/ProfitAndLoss?start_date&end_date&accounting_method`; `GET .../reports/AgedReceivables` (also AgedReceivableDetail). Both are standard reports. I could not fetch the Intuit reference pages (403); paths and parameters are from memory plus third-party docs and should be confirmed in the API Explorer. Invoice and Customer are read through query (`SELECT * FROM Invoice ...`) or `GET .../invoice/<id>`, `.../customer/<id>`. `companyinfo/<realmId>` for the company name.
- Minor versions: from 2025-08-01 Intuit deprecated minor versions 1-74; all requests default to 75, and a lower `minorversion` is ignored ([Intuit blog via search summary](https://blogs.intuit.com/?p=34635); [Codat](https://docs.codat.io/updates/250401-qbo-minor-versions-update)). Pin `minorversion=75` (or higher if newer is validated).
- Rate limits: 500 requests/min per realm; 10 requests/s per realm and app; batch 40/min per realm and app; HTTP 429, wait 60 s; requests over 120 s time out; combined 800/min per realm and app in production if the app also calls non-QBO endpoints ([limits and throttles](https://static.developer.intuit.com/output_html/qbo/docs/learn/limits-and-throttles.html)). The spec's 3 retries plus `Retry-After` need to cope with "wait 60 seconds".

### 5. Recent changes (2025-2026)
- 2025-07-28 App Partner Program live; Core/CorePlus metering; Builder 500k cap (above).
- 2025-08-01 minor versions below 75 retired.
- 2025-10-14 updated Developer Terms (AI, consent, processor obligations) per [Vorp Labs](https://vorplabs.com/agent-tools/quickbooks-online-api); worth reading before shipping an AI-facing connector (unread).
- After 2026-06-30 all Reports API responses come from a modernized reporting service: same URL/body, but structure, row order, grouping and fields may differ; only documented report endpoints stay supported; P&L stays supported; AgedReceivables not named ([G-Accon, 2026-06-15](https://g-accon.com/quickbooks-online-reports-api-is-changing/), citing an Intuit post I could not reach). fiscl should parse P&L defensively (use the report's Summary/total rows by group name, never row index) and store the raw totals only.
- Intuit published a QuickBooks Online MCP server (preview 2025-10-21, sandbox-only per Vorp Labs) with write tools toggled by env flags; not read-only at the OAuth level, same as fiscl's situation.

## Options compared
| Option | Fit | Cost | Risk | Notes |
|---|---|---|---|---|
| A. Build full loopback OAuth for sandbox and production | Poor: production rejects localhost | $0 | High: breaks for real books | Needs a public HTTPS callback; violates local-first |
| B. Sandbox loopback + production via paste-refresh-token (OAuth Playground) | Good | $0 | Medium: Playground availability for production keys unverified | Matches spec fallback; user does one-time manual step |
| C. Sandbox-only | Weak value (sample data only) | $0 | Low | Useful as a development/test target only |
| D. Drop | None | $0 | None | Loses the books-vs-bank feature |

## Recommendation
BUILD SANDBOX-ONLY first, then enable production behind one human check.
1. Implement slice 5 against sandbox with the loopback flow on `http://localhost:<port>/qbo/callback` (not `127.0.0.1`), `state` only (no PKCE), GET-only allowlist, `minorversion=75`, rotating-refresh handling, 429 backoff of 60 s.
2. For production, make `--paste-refresh-token` the primary documented path (the user obtains a token for their own production app, which needs the self-attested questionnaire), and keep loopback as sandbox-only. Do not ask users to run ngrok or host a public callback.
3. Before enabling production in a release, a human must confirm: (a) the production questionnaire can be completed for a personal single-company app; (b) the Intuit OAuth Playground (its https redirect) works with production keys for such an app; (c) the exact `reports/AgedReceivables` and `ProfitAndLoss` parameters in the API Explorer. If (b) fails, there is no free local-only production path, and the answer becomes DROP for production (keep sandbox as a test harness only).
Spec edits: use `localhost` not `127.0.0.1`; drop PKCE; document 100-day refresh inactivity limit; add a "reports format may change" note; add `AgedReceivables` to the report allowlist if wanted.

## Unverified
Intuit's reference pages (query, reports, minor versions, go-live, help threads) returned 403/JS shells; these items rely on search snippets or third-party sources: questionnaire contents, production localhost rejection (corroborated by multiple sources but not read in Intuit's text), POST /query, report parameters, AgedReceivables status after the 2026-06-30 migration.

## Sources
- [Intuit App Partner Program Guide v1.2 (03.2026)](https://static.developer.intuit.com/resources/Intuit_App_Partner_Program_Guide.pdf) — accessed 2026-10-11
- [API call limits and throttles](https://static.developer.intuit.com/output_html/qbo/docs/learn/limits-and-throttles.html) — 2026-10-11
- [OAuth 2.0 (static mirror)](https://static.developer.intuit.com/output_html/qbo/docs/develop/authentication-and-authorization/oauth-2.0.html) — 2026-10-11
- [Authorization FAQ (static mirror)](https://static.developer.intuit.com/output_html/qbo/docs/develop/authentication-and-authorization/faq.html) — 2026-10-11
- [Set app redirect URIs (static mirror)](https://static.developer.intuit.com/output_html/qbo/docs/develop/authentication-and-authorization/set-redirect-uri.html) — 2026-10-11
- [OpenID discovery](https://developer.intuit.com/.well-known/openid_configuration) — 2026-10-11
- [intuit/quickbooks-online-mcp-server](https://github.com/intuit/quickbooks-online-mcp-server) — 2026-10-11
- [Apideck: QuickBooks API pricing](https://www.apideck.com/blog/quickbooks-api-pricing-and-the-intuit-app-partner-program) — 2026-10-11
- [Truto: QuickBooks API cost 2026](https://truto.one/blog/how-much-does-the-quickbooks-api-cost-2026-pricing-rate-limits/) — 2026-10-11
- [Vorp Labs: QBO API review/metering](https://vorplabs.com/agent-tools/quickbooks-online-api) — 2026-10-11
- [G-Accon: Reports API change](https://g-accon.com/quickbooks-online-reports-api-is-changing/) — 2026-10-11
- [Codat: minor versions update](https://docs.codat.io/updates/250401-qbo-minor-versions-update) — 2026-10-11
- [Intuit blog: Deep dive SQL queries](https://blogs.a.intuit.com/2017/02/08/deep-dive-sql-queries) — 2026-10-11
