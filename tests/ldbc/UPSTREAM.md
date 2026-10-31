# Upstream sources of the LDBC corpus

> These are not LDBC Benchmark Results. This directory uses the LDBC Social
> Network Benchmark's data and reference SPARQL queries as a validation and
> performance corpus; it is not an implementation of an LDBC Benchmark.
> See `docs/architecture/ldbc_benchmark_plan.md` and PRD §13.6.

## Queries

- **Repository:** <https://github.com/ldbc/ldbc_snb_interactive_v1_impls>
- **Commit:** `11db98c` (2026-03-26); `graphdb/queries/` last changed in `7c57d8f` (2022-12-13)
- **Path:** `graphdb/queries/interactive-{short,complex}-*.rq` (the 21 reads; the
  update operations are out of scope because this service is read-only)
- **Licence:** Apache-2.0, Copyright the Linked Data Benchmark Council (see `NOTICE`)

`queries/interactive_v1/` holds the files byte for byte. They are never edited;
`test_ldbc_ratchet.py` checks each against the digest below. Adaptations live in
`queries/variants/<op>.<reason>.rq`, each with a header saying what changed and
why.

| File | SHA-256 |
| --- | --- |
| `interactive-short-1.rq` | `cf9fed8ccec7e17b157706827aa48a2f0ce776c21eb24131b24152ad98e25ae5` |
| `interactive-short-2.rq` | `9b7e4dd55dd10e3ba1fc5b42cf87f88296fce27efc8e369f9acc50dd739b3384` |
| `interactive-short-3.rq` | `23ee1fafda791590cc54062a99647d333ba0b4c40374f240378df09b71161e01` |
| `interactive-short-4.rq` | `e5fead7281719d7da752b7ffd7313e2ade8e7fbd6a9ada3cfed477525a5aa708` |
| `interactive-short-5.rq` | `8a6d73385168ac9ded89646856cb3973a77d94fcb28ce29a184b255a9fbc4f29` |
| `interactive-short-6.rq` | `b00ecc7b7e99f7a1922040beabd40cedac51c313ec91cca151440ad8fb840e3b` |
| `interactive-short-7.rq` | `d0090a06848e09fc42d074f841fbebc31061f3cad207b01950166764b87e2354` |
| `interactive-complex-1.rq` | `2247ef5f0c75aebe83340ed3f1791bfd18f404e3da29b5c1c0552dced3b47550` |
| `interactive-complex-2.rq` | `fa011cef0dd7e70699d1c993bc0c14026a5d773740d8f3b759eaa777c42954f4` |
| `interactive-complex-3.rq` | `593218e41e7d1fede99c9b1105fc15c0d584ebdcd4bb04f498915d12ebcc0629` |
| `interactive-complex-4.rq` | `d1e92b0310152f401ff1928321a8d1783da4e459481d5e11f87852cf70535d94` |
| `interactive-complex-5.rq` | `927babb2a53291b6cd0764c13f9733ffb2777d4330489f86e132ac1d1f03cda3` |
| `interactive-complex-6.rq` | `d0403e7ac9e09f16db7e21ac52f50953d59f90ba13cb16457bb6ec02bd21da39` |
| `interactive-complex-7.rq` | `ddf7e7e11c4f7135fed69d584c830dc043c7e8785d2ff02caaa0d9b073e0bcfe` |
| `interactive-complex-8.rq` | `eb0134addc08ae8783293cf5a246a720127e3deab98b51bfc45bd4bf3d46012b` |
| `interactive-complex-9.rq` | `672fa32103dc63bb8a53221ff22b53fbd868f0985d1ff5df5e159c8b2c21e97f` |
| `interactive-complex-10.rq` | `b5dda3dfd132c5d58681bdc0f084d2136eb17eaa9a1a3c4a9d104d49742616c2` |
| `interactive-complex-11.rq` | `d4698508aecde061cfb40ccf8175d74398e9e0d06d4d55bd65bfdd0a103356e0` |
| `interactive-complex-12.rq` | `095c9c6a90d307e13d499dd783856682d5aa6acac1e148352cc11c292003c9c6` |
| `interactive-complex-13.rq` | `496baa2fea9c7b2b3eb3528e6240eeb6d3edafdf0e78ab092f52d6994d135acf` |
| `interactive-complex-14.rq` | `3f6e37a6e45859d4ecf581f41193a19b945cda03c5695e118e1db5005bd65cdc` |

### Refreshing

1. Pick the new upstream commit and download the 21 files from
   `graphdb/queries/` at that commit.
2. Replace `queries/interactive_v1/`, update the commit above and the digests
   (`shasum -a 256`).
3. Check each variant still applies, then run
   `python -m tests.ldbc.ledger --write` and review the ledger diff.

## Data

- **Data sets:** <https://ldbcouncil.org/benchmarks/snb/datasets/>, the
  Interactive v1 Turtle serialisation (`social_network-sf{SF}-Turtle.tar.zst`,
  Datagen v1.0.0), and the official substitution parameters. URLs and SHA-256
  pins are in `datasets.py`. Nothing is vendored.
- **Mirror:** `LDBC_DATASET_MIRROR` names a base URL tried first, serving each
  archive at the same path as on `datasets.ldbcouncil.org`; its file must
  match the same pin. The nightly job reads it from the repository variable
  of that name, because LDBC's server answered the GitHub-hosted runner with
  HTTP 403 on 2026-10-28.
- **Data quirk:** the Turtle gives the country and the continent "Australia" the
  same IRI (`dbpedia:Australia`, typed both `Country` and `Continent`), where
  the CSV export has two entities. LPG keeps both labels; PG keeps the node in
  `country` (`vocab.PG_CLASS_PRIORITY`).
- **Validation parameters:** LDBC publishes expected results for Interactive v1
  (`validation_params-interactive-v1.0.0-sf0.1-to-sf10.tar.zst`, SHA-256
  `1fc17c19426c2ea04e4b2119b28a8813323b8d0b4f7a6731bb7b8445dd9cf707`). They
  interleave 7,996 inserts with 130,478 reads from the first line, so they
  describe the database *after* inserts the Turtle snapshot does not contain.
  `python -m tests.ldbc.replay` replays the inserts on the oracle and checks
  the short reads against them (plan, WP-LDBC-REPLAY).
