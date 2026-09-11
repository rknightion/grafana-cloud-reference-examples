# Test fixtures

1,036 lines of Adobe Experience Manager as a Cloud Service log data, used by
`../tests/` and by `../dev/run_local.py`.

## Provenance

| File | Lines | Source |
|---|---|---|
| `author_aemaccess.log` | 250 | real, sanitised |
| `author_aemerror.log` | 250 | real, sanitised |
| `author_aemrequest.log` | 250 | real, sanitised |
| `author_aemcdn.log` | 250 | real, sanitised |
| `publish_aemdispatcher.log` | 15 | hand-written from Adobe's documented format |
| `publish_aemhttpdaccess.log` | 11 | hand-written from Adobe's documented format |
| `publish_aemhttpderror.log` | 10 | hand-written from Adobe's documented format |

The four real files are a single author-tier day, down-sampled from 22k-46k
lines each. The three hand-written ones exist because no real publish-tier or
Apache-layer sample was available; they match the documentation rather than
observed output, and the example README says so.

## Sanitisation

Produced by `tools/sanitise_aem_logs.py`, run as
`just sanitise-aem-logs <directory>`. Every identifier is rewritten:

| Real | Replacement |
|---|---|
| Cloud Manager program and environment ids | `p12345`, `e67890` |
| AEM tenant slug | `examplecorp` |
| Pod and ReplicaSet names | deterministic per-pod fakes |
| Authenticated user email addresses | `user<n>@example.com` |
| Public client addresses | RFC 5737 TEST-NET-3 (`203.0.113.0/24`) |
| Private cluster addresses | RFC 1918 (`10.0.0.0/16`) |
| IPv6 client addresses | RFC 3849 (`2001:db8::/32`) |
| Request UUIDs | deterministic well-formed v4 UUIDs |
| DAM asset filenames | `ASSET-<year>-<n>_<hash>.<ext>` |
| DAM content paths | one opaque segment |

### IP addresses are replaced here, and kept by default everywhere else

`tools/sanitise_aem_logs.py` defaults to `--ip-addresses keep`, because a client
address in an operational log is normal telemetry and the shipped Lambda sends
them to Loki by default too. This corpus is the exception: it goes into a
**public repository**, so those addresses would be real visitors to a customer's
site republished somewhere they were never meant to go. The `just` recipe passes
`--ip-addresses replace` for exactly that reason.

### Asset names were the finding that mattered

AEM's asset-processing log names the asset it is working on, and an asset name is
unconstrained customer content. The source logs carried **608 distinct
filenames** including the customer's product brand names and their subsidiary's
name - none of which any identifier pattern catches, because they are free text
rather than an id. Two had already reached the corpus before the content check
was added.

They are now rewritten to a stable placeholder that keeps Adobe's
`ASSET-<year>-<number>` shape and the extension, so a fixture still exercises
the parser and a dashboard grouping by asset still shows a realistic spread.

### The sanitiser refuses to write high-risk PII

Medical and health data, home addresses, dates of birth, phone numbers, national
identifiers and payment cards are **detected and refused**, not rewritten. A
heuristic replacement of a medical record number gives false confidence; the
right response is to look at why it is being logged at all. `--allow-sensitive`
overrides it, and every match should be a confirmed false positive before you
reach for it.

Both detectors that needed it are context-scoped rather than shape-only, because
a shape-only version fired on every line: the ten-digit NHS shape matched an
Apache microsecond timestamp plus year, and the card shape matched a fifteen-digit
Apache thread id which then passed the Luhn check by coincidence.

`127.0.0.1` is kept verbatim. It appears in every AEM access log from the JVM's
own health check, it identifies nobody, and keeping it preserves the
internal-versus-external distinction the dashboards rely on.

Substitution is **deterministic**: the same input token always maps to the same
placeholder, derived from a keyed hash. That keeps regeneration from producing a
completely different diff every time, and keeps an access log line correlated
with the CDN line for the same request.

**The keyed hash is not a privacy mechanism.** The key is a constant in the
script, so anyone can recompute the mapping. It exists for stability. Privacy
comes from the values being replaced, and from the publication scan refusing
anything that was missed.

## Regenerating

```bash
just sanitise-aem-logs ~/Downloads/some-aem-logs
just public-release-scan
```

**The scan is the gate, not the sanitiser.** The sanitiser is pattern-based and
cannot know a customer-specific string it has no rule for. `tests/test_aem_fixtures.py`
also asserts that no fixture carries a real program id, a non-placeholder email
domain, or an address outside the documentation and private ranges - those
failures name the file and line, which is a faster diagnosis than a scan hit.

## Why the sampling takes contiguous blocks

Stride sampling - every Nth line - destroys adjacency, and adjacency carries
meaning in these formats. The request log pairs a `->` line with a `<-` line, and
the error log follows an exception with its stack-trace continuation lines.

A stride-sampled corpus paired 6 of 137 responses where the full file pairs all
of them, so a test written against it would have encoded the sampler's artefact
as the expected behaviour. Blocks are taken from evenly spaced offsets so the
corpus still spans the whole day, and the final block comes from the end of the
file, which is where the genuinely unpairable response-without-request cases are.

`tests/test_aem_fixtures.py` asserts the corpus keeps that variety: more than one log
level, at least one non-2xx status, several cache outcomes and countries, high
but not total correlation, and at least one uncorrelated response.
