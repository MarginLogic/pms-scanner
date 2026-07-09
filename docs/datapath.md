# Scanned-file data path

Traces a single scanned file from the moment it lands in an environment's watch
folder until every page is uploaded to the backend. Source of truth: the
`scanner/` package (`scheduler.py`, `batch.py`, `pdf_processor.py`,
`uploader.py`, `state.py`, `config.py`).

## Key facts

- **One file → one environment.** Routing is by watch folder; two enabled envs
  cannot share a `watch_dir`. Fan-out is at the *job* level — one `BatchRunner`
  per `(machine, environment)`, one APScheduler job per enabled env per machine.
- **Claim is an atomic `os.rename`** into `in-progress/<machine>/`, arbitrating
  between machines that share an SMB watch folder. A lost race just means a peer
  got the file.
- **300 DPI render** (`zoom = 300/72`) with two-tier orientation: PDF `page.rotation`
  metadata first, then Tesseract OSD only if metadata reports 0°.
- **Upload is per page**, TIFF/LZW lossless, rate-limited to 60 requests / 60s,
  retries 5xx and network errors (≤3, exponential backoff capped at 10s) but
  **never retries 4xx**.
- **Disposition:** all pages OK → `processed/`; any failure or exception → file
  goes **back to `watch_dir`** (no dedicated error folder) and is retried on a
  later poll. On startup, `recover_stranded()` returns files stranded in this
  machine's `in-progress/<machine>/` back to the watch dir.

## Diagram

```mermaid
flowchart TD
    scan([Scanner drops file<br/>.pdf / .tif / .tiff]) --> watch[/"watch_dir<br/>(per environment)"/]

    subgraph startup["Process startup (once)"]
      recover["recover_stranded()<br/>scan in-progress/&lt;machine&gt;/ only<br/>rename stranded files → watch_dir<br/>(.recovered-&lt;UTC&gt; on collision)"]
    end
    recover --> watch

    subgraph sched["APScheduler — one CronTrigger job per (machine, environment)"]
      tick["Job fires each minute at<br/>env.schedule_offset_seconds<br/>max_instances=1, coalesce=True"]
    end
    watch --> tick
    tick --> runonce["BatchRunner.run_once()<br/>mark_run_started → emit run_started"]

    runonce --> settle{"_find_settled()<br/>supported ext?<br/>mtime ≥ settle_seconds (10s)?"}
    settle -->|no| skipd["skip — file still being written<br/>or unsupported"]
    settle -->|yes| claim{"claim_file()<br/>atomic os.rename →<br/>in-progress/&lt;machine&gt;/"}

    claim -->|rename fails| peer["peer machine won race<br/>→ skip (fleet-safe claim)"]
    claim -->|success| inprog[/"in-progress/&lt;machine&gt;/file"/]

    inprog --> proc["process_pdf() — PyMuPDF / PIL"]

    subgraph pp["pdf_processor — per page"]
      orient["Orientation detect:<br/>Tier 1 page.rotation metadata<br/>Tier 2 pytesseract OSD (if 0°)"]
      render["Render @ 300 DPI<br/>zoom = 300/72 → RGB pixmap"]
      rot["rotate(-rotation, expand=True)<br/>flag orientation_uncertain on OSD fail"]
      orient --> render --> rot
    end
    proc --> pp
    pp --> pages["list of (page_num, PIL.Image, ...)"]

    pages --> uploadloop["for each page: upload_page()"]

    subgraph up["uploader — per page"]
      enc["encode TIFF (LZW, lossless)<br/>name = {stem}_p{NNN}.tiff"]
      rl["rate limit: ≤ 60 req / 60s"]
      post["POST multipart →<br/>{backend_base_url}/api/scanned-images/upload<br/>header X-API-Key"]
      enc --> rl --> post
    end
    uploadloop --> up

    up --> route{"env routing<br/>(backend_base_url)"}
    route -->|production| prod[("https://adg.mpsinc.io")]
    route -->|staging| stg[("https://dev.adg.mpsinc.io")]

    post --> resp{"response?"}
    resp -->|"2xx, images[] non-empty"| ok["accepted<br/>add_pages_uploaded(1)<br/>emit page_done"]
    resp -->|"5xx / network err"| retry["retry ≤ 3x<br/>exp backoff min(2^n, 10s)"]
    retry --> post
    resp -->|"4xx (never retried)"| fail["page failed<br/>add_error()"]

    ok --> allok{"all pages<br/>uploaded?"}
    fail --> allok
    retry -.->|retries exhausted| fail

    allok -->|yes| processed[/"os.rename → processed/<br/>add_files_processed(1)<br/>status = completed"/]
    allok -->|no / exception| back[/"os.rename → back to watch_dir<br/>status = failed → retried next poll"/]

    processed --> finalize["finally: clear current_file<br/>emit file_done"]
    back --> finalize

    finalize --> finish["mark_run_finished → emit run_done"]

    style prod fill:#2b7a2b,color:#fff
    style stg fill:#7a5a2b,color:#fff
    style processed fill:#1f4e79,color:#fff
    style back fill:#8b2b2b,color:#fff
    style scan fill:#444,color:#fff
```
