# Native HTTPS benchmark client experiment

## Scope and safety

This is an experiment on branch `experiment/native-benchmark-client`, based on
`8f702dc066e953a1afeb5282643cf6fcc2643c1f`. Production code still defaults to
curl; the branch adds an explicit `client="native"` option for the benchmark
runner. No production deployment, DNS rewrite, production DB, or merge to main
was part of this experiment.

The prototype uses Python's standard `socket`, `ssl`, and `http.client` stack.
Each sample opens a TCP socket directly to the candidate IP, wraps that socket
with the original target hostname as TLS SNI, uses Python's default CA trust
context and hostname verification, sends one HTTP/1.1 GET with the original
Host header and `Connection: close`, reads the full response body, then closes
the connection. It accepts HTTP 200–399 and does not follow redirects. TCP,
TLS, and total request timings remain separate benchmark fields. A total
deadline watchdog and bounded connect timeout are covered by local tests.

Local TLS regression tests use a test-only trusted CA fixture; production
contexts use `ssl.create_default_context()` with the machine's default trust
store. The experiment was run on Windows with Python 3.12.10 and the installed
Windows curl. The curl path has a Windows Schannel-specific retry for
`CRYPT_E_REVOCATION_OFFLINE` that the native OpenSSL-based client does not
replicate. Trust-store and revocation backend differences must be considered
before any platform-wide replacement.

## Reproduction

Run from the repository root:

```powershell
python scripts/native_client_ab.py
```

The harness discovers public candidate IPs once, then gives the exact same
ordered IP list to each client. It alternates curl/native order over five
repeats per case. Both clients perform ten GET samples per candidate with a
10-second timeout. Case C uses two concurrent target workers. It makes only
public HTTPS GET requests and never calls an AdGuard mutation endpoint.

## Measurements

Final five-repeat network measurements are recorded below. Batch and candidate
figures are medians; sample totals aggregate all five repeats. CPU is Python
parent CPU plus measured curl-child CPU where applicable. RSS reports the
Python process high-water mark and the largest individual curl child working
set separately; these are not a simultaneous aggregate peak.

| Case | Client | Median batch wall | Median candidate work | Samples valid | Python CPU total | Curl child CPU total | Parent peak RSS* | Largest curl child RSS* |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A: 1 target, 2 IPs × 10 | curl | 5,859 ms | 5,859 ms | 100/100 | 203 ms | 2,547 ms | 43.2 MiB | 8.3 MiB |
| A | native | 7,118 ms (+21.5%) | 7,118 ms | 100/100 | 2,859 ms | 0 | 43.3 MiB | — |
| B: 1 target, 4 IPs × 10 | curl | 14,938 ms | 14,938 ms | 200/200 | 812 ms | 5,109 ms | 46.2 MiB | 8.3 MiB |
| B | native | 15,220 ms (+1.9%) | 15,219 ms | 200/200 | 5,344 ms | 0 | 45.7 MiB | — |
| C: 2 targets concurrently, 2 IPs × 10 each | curl | 7,431 ms | 6,658 ms / target | 200/200 | 609 ms | 4,922 ms | 50.0 MiB | 8.3 MiB |
| C | native | 7,595 ms (+2.2%) | 7,331 ms / target | 200/200 | 5,312 ms | 0 | 50.2 MiB | — |

*Memory is sampled from one shared Windows Python process across all runs, so
the Python parent peak is a process high-water mark rather than an isolated
per-client comparison. Curl child peak is measured per curl process. Case C's
simultaneous aggregate parent-plus-child peak was not measured. Curl's parent
and child CPU are both included in total CPU; native CPU is in the Python
process. Totals cover five repeats of the case.

Across all samples, median `(TCP connect, TLS, total)` milliseconds were:

| Case | curl | native |
| --- | --- | --- |
| A | (68.7, 82.5, 232.4) | (70.2, 97.2, 358.8) |
| B | (36.4, 72.3, 306.9) | (36.2, 87.8, 371.1) |
| C | (57.2, 78.7, 272.0) | (57.4, 94.2, 358.3) |

The native client used substantially more Python-parent CPU (about 2.9–14×
the curl parent depending on case). Once curl child CPU is included, total CPU
is close: A 2,750 ms curl / 2,859 ms native; B
5,921 / 5,344 ms; C 5,531 / 5,312 ms. It did not yield a consistent throughput
gain. Every sample was valid for both clients (500 per client); there were no
timeouts or rejected statuses in this measurement window.

### Candidate sets and ranking stability

The one-time public discovery returned these candidates; the tests used the
first two for A and C and the first four for B:

- `example.com`: `172.66.147.243`, `104.20.23.154`
- `www.google.com`: `142.251.157.119`, `142.251.156.119`, `142.251.152.119`, `142.251.155.119`

Paired best-IP agreement by repeat was A 3/5, B 1/5, and C 2/10 target-run
pairs. Candidate timing is close enough that the selected winner often changed
between adjacent repeats; the experiment does not show stable ranking
equivalence for candidates whose latency is similar.

Candidate ranking uses DRO's existing `rank_candidates` order (healthy only,
average then median then jitter). Timing-based winner differences are expected
when candidates have close latency; the paired winner agreement is reported
above as a measure of noise, not as a functional correctness check.

## Correctness coverage

`tests/test_native_benchmark.py` checks direct IP pinning, original Host and
SNI, CA and hostname verification, a new connection for each sample, accepted
3xx with no redirect follow, rejected status classification, read timeout, and
total deadline enforcement while response headers trickle. Existing
statistics, benchmark-health, and instrumentation tests also run.

## Assessment

**KEEP CURL.** The native path preserved the tested security and HTTP policies,
but total-request timing was consistently slower in these runs (especially
Case A), it did not improve overall CPU consistently, and its top-candidate
ranking agreed with curl in only 6 of 20 paired comparisons. Python and curl
also use different TLS implementations/trust backends on this Windows
workstation, including curl's Schannel revocation-offline fallback. The lower
subprocess count alone is not enough reason to replace the current client.
Production remains on its unchanged curl-default path.
